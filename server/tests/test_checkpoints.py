from __future__ import annotations

import http.client
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from server.app import H3StudioServer, Handler, Runtime
from server.checkpoints import CheckpointManager
from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY
from server.storage import AssetStore, JobStore
from server.tests.test_app import FakeComfy, make_config
from server.workflows import ResumeSamplingPlan, compile_workflow, parse_generation_request


class ResumeWorkflowTests(unittest.TestCase):
    def test_initial_and_resume_graphs_split_one_fixed_schedule_and_never_add_new_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            config.prepare()
            profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base-resumable")
            request = {
                "output_type": "video", "profile_id": profile.id,
                "profile_version": profile.version, "profile_digest": profile.digest(),
                "director_mode": "t2v", "prompt": "A cinematic sunrise over mountains.",
                "parameters": {"steps": 7, "duration": 5, "aspect_ratio": "16:9", "seed": 42},
            }
            spec = parse_generation_request(request, lambda _id: {}, DEFAULT_REGISTRY)
            initial = compile_workflow(spec, config, "a" * 32, ResumeSamplingPlan("initial", 50))
            self.assertEqual(initial["12"]["inputs"]["steps"], 50)
            self.assertEqual(initial["18"]["inputs"]["step"], 7)
            self.assertEqual(initial["13"]["inputs"]["sigmas"], ["18", 0])
            self.assertEqual(initial["19"]["class_type"], "H3StudioSaveLatent")
            self.assertEqual(initial["19"]["inputs"]["samples"], ["13", 0])
            self.assertEqual(initial["19"]["inputs"]["video_done"], ["17", 0])
            self.assertEqual(initial["14"]["inputs"]["samples"], ["13", 1])
            resumed_spec = replace(spec, steps=10)
            resumed = compile_workflow(resumed_spec, config, "b" * 32, ResumeSamplingPlan(
                "resume", 50, steps_before=7, additional_steps=3,
                checkpoint_input="h3-studio/checkpoints/checkpoint.latent",
            ))
            self.assertEqual(resumed["9"]["class_type"], "DisableNoise")
            self.assertEqual(resumed["18"]["inputs"]["step"], 7)
            self.assertEqual(resumed["20"]["inputs"]["step"], 3)
            self.assertEqual(resumed["21"]["class_type"], "H3StudioLoadLatent")
            self.assertEqual(resumed["13"]["inputs"]["latent_image"], ["21", 0])
            self.assertEqual(resumed["13"]["inputs"]["sigmas"], ["20", 0])

    def test_default_base_profile_is_direct_and_has_no_checkpoint_critical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            config.prepare()
            profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
            self.assertFalse(CheckpointManager.profile_policy(profile))
            spec = parse_generation_request({
                "output_type": "video", "profile_id": profile.id,
                "profile_version": profile.version, "profile_digest": profile.digest(),
                "director_mode": "t2v", "prompt": "A cinematic sunrise.",
                "parameters": {"steps": 20, "duration": 5, "aspect_ratio": "16:9", "seed": 42},
            }, lambda _id: {}, DEFAULT_REGISTRY)
            workflow = compile_workflow(spec, config, "f" * 32)
            self.assertEqual(workflow["17"]["class_type"], "SaveVideo")
            self.assertNotIn("18", workflow)
            self.assertNotIn("19", workflow)
            self.assertEqual(workflow["14"]["inputs"]["samples"], ["13", 0])


class CheckpointStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.config.prepare()
        self.config.comfy_output.mkdir(parents=True, exist_ok=True)
        self.jobs = JobStore(self.config.data_root / "metadata" / "jobs")
        self.assets = AssetStore(self.config)
        self.manager = CheckpointManager(self.config, self.jobs, self.assets, DEFAULT_REGISTRY, __import__("threading").RLock())
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base-resumable")
        self.job_id = "a" * 32
        self.job = {
            "id": self.job_id, "status": "completed", "chain_id": self.job_id,
            "prompt": "compiled prompt", "references": [],
            "parameters": {
                "profile_id": profile.id, "profile_version": profile.version,
                "profile_digest": profile.digest(), "steps": 7, "seed": 42,
                "sampler": "res_multistep", "scheduler": "simple", "denoise": 1,
                "width": 1344, "height": 768, "frames": 124,
            },
            "workflow_evidence": {"diffusion_model": self.config.fl_model, "lora": None, "lora_strength": 0},
            "created_at": time.time(),
        }
        self.jobs.put(self.job_id, self.job)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record(self, filename: str, content: bytes) -> dict:
        (self.config.comfy_output / filename).write_bytes(content)
        return {"outputs": {"19": {"latents": [{"filename": filename, "subfolder": "", "type": "output"}]}}}

    def test_atomic_latest_only_replacement_and_failed_attempt_preserves_old_checkpoint(self) -> None:
        first = self.manager.capture(self.job, self._record("first.latent", b"first"))
        self.assertEqual(self.manager.latest(self.job)["checkpoint_id"], first["checkpoint_id"])
        child_id = "b" * 32
        child = {**self.job, "id": child_id, "chain_id": self.job_id, "parent_job_id": self.job_id,
                 "parameters": {**self.job["parameters"], "steps": 9}, "created_at": time.time() + 1}
        self.jobs.put(child_id, child)
        # A failed/cancelled continuation never calls capture, so the old point remains valid.
        self.assertEqual(self.manager.latest(self.job)["checkpoint_id"], first["checkpoint_id"])
        second = self.manager.capture(child, self._record("second.latent", b"second"))
        self.assertNotEqual(first["checkpoint_id"], second["checkpoint_id"])
        self.assertFalse((self.manager.root / first["stored_name"]).exists())
        self.assertEqual((self.manager.root / second["stored_name"]).read_bytes(), b"second")
        self.assertEqual(len(list(self.manager.root.glob("*.latent"))), 1)

    def test_ttl_gc_restart_recovery_and_corruption_are_explicit(self) -> None:
        manifest = self.manager.capture(self.job, self._record("restart.latent", b"latent"))
        restarted = CheckpointManager(self.config, self.jobs, self.assets, DEFAULT_REGISTRY, __import__("threading").RLock())
        self.assertEqual(restarted.latest(self.job)["sha256"], manifest["sha256"])
        path = restarted.root / manifest["stored_name"]
        path.write_bytes(b"damaged")
        with self.assertRaises(ApiError) as corrupt:
            restarted.latest(self.job)
        self.assertEqual(corrupt.exception.code, "checkpoint_corrupt")
        path.write_bytes(b"latent")
        expired = {**manifest, "expires_at": time.time() - 1}
        restarted.metadata.put(self.job_id, expired)
        result = restarted.garbage_collect()
        self.assertEqual(result["manifests"], 1)
        self.assertFalse(path.exists())

    def test_reviewed_legacy_ref_profile_identity_keeps_checkpoint_resume_compatible(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va-base-resumable")
        legacy_version, legacy_digest = profile.compatible_identities[0]
        self.job["parameters"].update({
            "profile_id": profile.id,
            "profile_version": legacy_version,
            "profile_digest": legacy_digest,
        })
        self.job["workflow_evidence"]["diffusion_model"] = self.config.ref_model
        self.jobs.put(self.job_id, self.job)
        manifest = self.manager.capture(self.job, self._record("legacy.latent", b"legacy"))
        self.assertIsNotNone(manifest)
        resumed = self.manager.build_spec(self.job, steps=9)
        self.assertEqual(resumed.profile_version, profile.version)
        self.assertEqual(resumed.profile_digest, profile.digest())


class ResumeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.config.prepare()
        self.config.comfy_output.mkdir(parents=True, exist_ok=True)
        self.fake = FakeComfy()
        runtime = Runtime(self.config, AssetStore(self.config), JobStore(self.config.data_root / "metadata" / "jobs"), self.fake)  # type: ignore[arg-type]
        self.server = H3StudioServer(("127.0.0.1", 0), Handler, runtime)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method: str, path: str, value: dict | None = None):
        body = json.dumps(value).encode() if value is not None else None
        headers = {"X-API-Key": "test-key"}
        if body is not None: headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, json.loads(raw)

    def _complete(self, job_id: str, suffix: str) -> dict:
        video = f"{suffix}.mp4"; latent = f"{suffix}.latent"
        (self.config.comfy_output / video).write_bytes(b"video")
        (self.config.comfy_output / latent).write_bytes(("latent-" + suffix).encode())
        self.fake.record = {
            "status": {"completed": True},
            "outputs": {
                "17": {"videos": [{"filename": video, "subfolder": "", "type": "output"}]},
                "19": {"latents": [{"filename": latent, "subfolder": "", "type": "output"}]},
            },
        }
        return self.request("GET", f"/api/status?id={job_id}")[1]

    def test_any_chain_id_uses_latest_checkpoint_and_chain_rejects_parallel_resume(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base-resumable")
        status, created = self.request("POST", "/api/generate", {
            "output_type": "video", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
            "director_mode": "t2v", "prompt": "A cinematic sunrise over mountains.",
            "parameters": {"steps": 7, "duration": 5, "aspect_ratio": "16:9", "seed": 42},
        })
        self.assertEqual(status, 202)
        root_id = created["job_id"]
        self.assertEqual(self.fake.workflow["19"]["class_type"], "H3StudioSaveLatent")
        completed = self._complete(root_id, "root")
        self.assertTrue(completed["can_resume"])
        status, resumed = self.request("POST", f"/api/jobs/{root_id}/resume", {"additional_steps": 2, "request_id": "resume-request-1"})
        self.assertEqual(status, 202)
        child_id = resumed["job_id"]
        self.assertEqual((resumed["steps_before"], resumed["steps_after"]), (7, 9))
        self.assertEqual(self.fake.workflow["9"]["class_type"], "DisableNoise")
        self.fake.record = None
        pending = self.request("GET", f"/api/status?id={child_id}")[1]
        self.assertFalse(pending["can_resume"])
        self.assertEqual(pending["resume_unavailable_reason"], "checkpoint_pending")
        status, busy = self.request("POST", f"/api/jobs/{root_id}/resume", {"additional_steps": 1, "request_id": "resume-request-2"})
        self.assertEqual(status, 409)
        self.assertEqual(busy["error"]["code"], "resume_chain_busy")
        self._complete(child_id, "child")
        status, continued = self.request("POST", f"/api/jobs/{root_id}/resume", {"additional_steps": 3, "request_id": "resume-request-3"})
        self.assertEqual(status, 202)
        self.assertEqual(continued["parent_job_id"], child_id)
        self.assertEqual((continued["steps_before"], continued["steps_after"]), (9, 12))

    def test_checkpoint_failure_after_video_keeps_primary_result_completed(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base-resumable")
        status, created = self.request("POST", "/api/generate", {
            "output_type": "video", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
            "director_mode": "t2v", "prompt": "A cinematic sunrise.",
            "parameters": {"steps": 7, "duration": 5, "aspect_ratio": "16:9", "seed": 42},
        })
        self.assertEqual(status, 202)
        job_id = created["job_id"]
        video = "primary.mp4"
        (self.config.comfy_output / video).write_bytes(b"video")
        self.fake.record = {
            "status": {"completed": True},
            "outputs": {"17": {"videos": [{"filename": video, "subfolder": "", "type": "output"}]}},
        }
        result = self.request("GET", f"/api/status?id={job_id}")[1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outputs"][0]["filename"], video)
        self.assertEqual(result["checkpoint_error"]["code"], "checkpoint_missing")
        self.assertFalse(result["can_resume"])

    def test_default_direct_base_completes_without_checkpoint_and_cannot_resume(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
        status, created = self.request("POST", "/api/generate", {
            "output_type": "video", "profile_id": profile.id,
            "profile_version": profile.version, "profile_digest": profile.digest(),
            "director_mode": "t2v", "prompt": "A cinematic sunrise.",
            "parameters": {"steps": 20, "duration": 5, "aspect_ratio": "16:9", "seed": 42},
        })
        self.assertEqual(status, 202)
        self.assertNotIn("19", self.fake.workflow)
        job_id = created["job_id"]
        video = "direct.mp4"
        (self.config.comfy_output / video).write_bytes(b"video")
        self.fake.record = {
            "status": {"completed": True},
            "outputs": {"17": {"videos": [{"filename": video, "subfolder": "", "type": "output"}]}},
        }
        completed = self.request("GET", f"/api/status?id={job_id}")[1]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["resume_unavailable_reason"], "profile_not_tested")
        status, body = self.request("POST", f"/api/jobs/{job_id}/resume", {
            "additional_steps": 1, "request_id": "direct-resume-unsupported",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "resume_unsupported")

    def test_unsupported_profile_missing_checkpoint_and_max_steps_fail_without_restart(self) -> None:
        turbo = DEFAULT_REGISTRY.get("minimax-h3-fl2va")
        job_id = "d" * 32
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "status": "completed", "prompt": "x", "references": [],
            "parameters": {"profile_id": turbo.id, "profile_version": turbo.version, "profile_digest": turbo.digest(), "steps": 4},
            "created_at": time.time(),
        })
        status, body = self.request("POST", f"/api/jobs/{job_id}/resume", {"additional_steps": 1, "request_id": "resume-unsupported"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "resume_unsupported")


if __name__ == "__main__":
    unittest.main()
