from __future__ import annotations

import http.client
import hashlib
import base64
import json
import tempfile
import threading
import unittest
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.app import H3StudioServer, Handler, Runtime
from server.config import Config
from server.errors import ApiError
from server.storage import AssetStore, JobStore
from server.profiles import DEFAULT_REGISTRY


def make_config(root: Path) -> Config:
    return Config(
        host="127.0.0.1",
        port=0,
        api_key="test-key",
        cors_origins=("http://localhost:3000",),
        comfy_url="http://unused",
        data_root=root / "data",
        comfy_input=root / "input",
        comfy_output=root / "output",
        max_json_bytes=262144,
        max_image_bytes=1024 * 1024,
        max_video_bytes=2 * 1024 * 1024,
        max_audio_bytes=1024 * 1024,
        max_asset_storage_bytes=10 * 1024 * 1024,
        max_active_jobs=4,
        asset_ttl_days=30,
        fl_model="fl.safetensors",
        ref_model="ref.safetensors",
        text_encoder="clip.safetensors",
        video_vae="video-vae.safetensors",
        audio_vae="audio-vae.safetensors",
        fl_lora="fl-lora.safetensors",
        ref_lora="ref-lora.safetensors",
        image_checkpoint="image.safetensors",
    )


class ConfigSecurityTests(unittest.TestCase):
    def test_checkpoint_ttl_is_bounded_to_the_supported_recovery_window(self) -> None:
        for value in ("23", "73"):
            with self.subTest(value=value), patch.dict(os.environ, {"H3_STUDIO_CHECKPOINT_TTL_HOURS": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "CHECKPOINT_TTL_HOURS"):
                    Config.from_env()

    def test_default_origins_are_local_gateway_origins(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
        self.assertEqual(
            config.cors_origins,
            ("http://127.0.0.1:3013", "http://localhost:3013"),
        )
        self.assertEqual(config.api_key, "")

    def test_default_storage_paths_are_workspace_local(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
        workspace = Path.cwd().resolve()
        self.assertEqual(config.data_root, workspace / "data")
        self.assertEqual(config.comfy_input, workspace / "comfy-input")
        self.assertEqual(config.comfy_output, workspace / "comfy-output")

    def test_wildcard_origin_requires_nonempty_key_for_env_and_runtime(self) -> None:
        with patch.dict(os.environ, {"H3_STUDIO_CORS_ORIGINS": "*"}, clear=True):
            with self.assertRaisesRegex(ValueError, "API_KEY"):
                Config.from_env()
        with tempfile.TemporaryDirectory() as temporary:
            unsafe = replace(make_config(Path(temporary)), api_key="", cors_origins=("*",))
            unsafe.prepare()
            with self.assertRaisesRegex(ValueError, "wildcard CORS"):
                Runtime(
                    unsafe, AssetStore(unsafe),
                    JobStore(unsafe.data_root / "metadata" / "jobs"),
                    FakeComfy(),  # type: ignore[arg-type]
                )

    def test_runtime_identity_is_persisted_with_the_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            config.prepare()
            first = Runtime(config, AssetStore(config), JobStore(config.data_root / "metadata" / "jobs"), FakeComfy())  # type: ignore[arg-type]
            second = Runtime(config, AssetStore(config), JobStore(config.data_root / "metadata" / "jobs"), FakeComfy())  # type: ignore[arg-type]
            self.assertRegex(first.instance_id, r"^[0-9a-f]{32}$")
            self.assertEqual(first.instance_id, second.instance_id)
            identity_path = config.data_root / "metadata" / "dataset-id"
            identity_path.unlink()
            third = Runtime(config, AssetStore(config), JobStore(config.data_root / "metadata" / "jobs"), FakeComfy())  # type: ignore[arg-type]
            self.assertNotEqual(first.instance_id, third.instance_id)


class FakeComfy:
    def __init__(self) -> None:
        self.workflow = None
        self.prompt_id = "comfy-prompt-id"
        self.record = None
        self.status_value = None
        self.submit_count = 0
        self.canceled: list[str] = []

    def health(self):
        return {"system": "fake"}

    def capabilities(self, _config, registry=None):
        return {
            "video": {"available": True}, "image": {"available": True},
            "profiles": [profile.public() for profile in registry.all()] if registry else [],
        }

    def ensure_capability(self, _spec, _config, _registry=None):
        return None

    def submit(self, workflow, _client_id):
        self.workflow = workflow
        self.submit_count += 1
        return self.prompt_id

    def cancel(self, prompt_id):
        self.canceled.append(prompt_id)

    def status(self, _prompt_id):
        if self.status_value:
            return {"status": self.status_value}
        if self.record:
            return {"status": "completed", "record": self.record}
        return {"status": "queued"}


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.config.prepare()
        self.fake = FakeComfy()
        runtime = Runtime(
            self.config,
            AssetStore(self.config),
            JobStore(self.config.data_root / "metadata" / "jobs"),
            self.fake,  # type: ignore[arg-type]
        )
        self.server = H3StudioServer(("127.0.0.1", 0), Handler, runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, *, timeout=3):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        headers_out = dict(response.getheaders())
        connection.close()
        return response.status, headers_out, content

    def test_health_is_public_but_assets_require_key(self) -> None:
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        status, _, body = self.request("GET", "/api/assets")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")

    def test_disallowed_browser_origin_cannot_read_or_mutate(self) -> None:
        evil = {"Origin": "https://evil.example", "X-API-Key": "test-key"}
        status, _, body = self.request("GET", "/api/assets", headers=evil)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "cors")
        payload = json.dumps({"name": "must-not-exist"}).encode()
        status, _, body = self.request(
            "POST", "/api/asset-folders", payload,
            {**evil, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "cors")
        self.assertEqual(self.server.runtime.folders.list(), [])

        allowed = {"Origin": "http://localhost:3000", "X-API-Key": "test-key"}
        status, headers, _ = self.request("GET", "/api/assets", headers=allowed)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://localhost:3000")

    def test_capabilities_expose_profiles(self) -> None:
        status, _, body = self.request("GET", "/api/capabilities", headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        profiles = json.loads(body)["profiles"]
        self.assertTrue(any(profile["id"] == "anything-v5-img2img" for profile in profiles))

    def test_character_migration_capability_and_plan_are_versioned_and_strict(self) -> None:
        source_id, character_id = "a" * 32, "b" * 32
        source_path = self.config.comfy_input / "h3-studio" / f"{source_id}.mp4"
        character_path = self.config.comfy_input / "h3-studio" / f"{character_id}.png"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"source")
        character_path.write_bytes(b"character")
        self.server.runtime.assets.metadata.put(source_id, {
            "id": source_id, "kind": "video", "filename": source_path.name,
            "stored_name": source_path.name, "comfy_path": f"h3-studio/{source_path.name}",
            "sha256": "c" * 64, "size": 1_000_000, "storage_size": 1_000_000,
            "media": {"duration": 60.0, "video_duration": 60.0, "fps": 24.0, "frame_count": 1440, "width": 1920, "height": 1080, "rotation": 0, "has_audio": True},
        })
        self.server.runtime.assets.metadata.put(character_id, {
            "id": character_id, "kind": "image", "filename": character_path.name,
            "stored_name": character_path.name, "comfy_path": f"h3-studio/{character_path.name}",
            "sha256": "d" * 64, "size": 1000, "storage_size": 1000,
            "media": {"width": 512, "height": 768},
        })
        profiles = []
        for profile in DEFAULT_REGISTRY.all():
            item = profile.public()
            item["available"] = True
            profiles.append(item)
        self.fake.capabilities = lambda _config, registry=None: {
            "video": {"available": True, "motion_context": {"available": True}},
            "image": {"available": True}, "profiles": profiles,
        }
        headers = {"X-API-Key": "test-key", "Content-Type": "application/json"}
        status, _, body = self.request("GET", "/api/capabilities", headers={"X-API-Key": "test-key"})
        migration = json.loads(body)["video"]["character_migration"]
        self.assertEqual(status, 200)
        self.assertTrue(migration["available"])
        self.assertEqual(migration["recipe_version"], "h3.character-migration/v1")
        payload = {
            "version": "h3.character-migration/v1", "source_asset_id": source_id,
            "targets": [{"character_asset_id": character_id, "source_subject": "the center performer"}],
            "profile_id": "minimax-h3-ref2va", "steps": 4,
            "segment_frames": 243, "overlap_frames": 39, "audio_policy": "copy-source",
        }
        status, _, body = self.request("POST", "/api/video/character-migration/plan", json.dumps(payload).encode(), headers)
        result = json.loads(body)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["windows"]), 7)
        self.assertEqual(result["project"]["recipe"]["version"], "h3.character-migration/v1")
        payload["unknown"] = True
        status, _, body = self.request("POST", "/api/video/character-migration/plan", json.dumps(payload).encode(), headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_character_migration")

    def test_voice_and_gpu_resource_routes_are_authenticated_and_stable(self) -> None:
        task_id = "e" * 32
        output = self.config.data_root / "voice-test.wav"
        output.write_bytes(b"RIFFvoice")
        dry_output = self.config.data_root / "voice-dry.wav"
        dry_output.write_bytes(b"RIFFdry")

        class FakeVoice:
            @staticmethod
            def submit(data):
                return {"id": task_id, "task_id": task_id, "status": "queued", "engine": data["engine"]}

            @staticmethod
            def get(_task_id):
                return {"id": task_id, "task_id": task_id, "status": "completed"}

            @staticmethod
            def list():
                return {"items": [{"id": task_id, "status": "completed"}]}

            @staticmethod
            def cancel(_task_id):
                return {"id": task_id, "status": "canceled"}

            @staticmethod
            def capabilities():
                return {"engines": [{"id": "vevo2", "available": True}]}

            @staticmethod
            def output_path(_task_id, track="mix"):
                if track == "dry_vocal":
                    return dry_output
                if track != "mix":
                    raise ApiError(404, "voice_track_missing", "voice output track was not retained")
                return output

            @staticmethod
            def stop():
                return None

        self.server.runtime.voice = FakeVoice()  # type: ignore[assignment]
        auth = {"X-API-Key": "test-key"}
        status, _, _ = self.request("GET", "/api/resources/gpus")
        self.assertEqual(status, 401)
        status, _, body = self.request("GET", "/api/resources/gpus", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["gpus"][0]["policy"], "fifo_no_preemption")
        status, _, body = self.request("GET", "/api/voice/capabilities", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["engines"][0]["id"], "vevo2")
        payload = json.dumps({
            "engine": "vevo2", "source_asset_id": "a" * 32,
            "reference_asset_id": "b" * 32,
        }).encode()
        status, _, body = self.request("POST", "/api/voice/tasks", payload, {**auth, "Content-Type": "application/json"})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["task_id"], task_id)
        status, _, body = self.request("GET", f"/api/voice/tasks/{task_id}", headers=auth)
        self.assertEqual((status, json.loads(body)["status"]), (200, "completed"))
        status, _, body = self.request("POST", f"/api/voice/tasks/{task_id}/cancel", headers=auth)
        self.assertEqual((status, json.loads(body)["status"]), (202, "canceled"))
        status, _, body = self.request("GET", f"/api/voice/tasks/{task_id}/download", headers=auth)
        self.assertEqual((status, body), (200, b"RIFFvoice"))
        status, preview_headers, preview = self.request("GET", f"/api/voice/tasks/{task_id}/preview?track=mix", headers=auth)
        self.assertEqual((status, preview), (200, body))
        self.assertIn("inline", preview_headers["Content-Disposition"])
        status, download_headers, downloaded = self.request("GET", f"/api/voice/tasks/{task_id}/download?track=mix", headers=auth)
        self.assertEqual((status, downloaded), (200, preview))
        self.assertIn("attachment", download_headers["Content-Disposition"])
        status, _, dry_preview = self.request("GET", f"/api/voice/tasks/{task_id}/preview?track=dry_vocal", headers=auth)
        self.assertEqual((status, dry_preview), (200, b"RIFFdry"))
        status, _, dry_download = self.request("GET", f"/api/voice/tasks/{task_id}/download?track=dry_vocal", headers=auth)
        self.assertEqual((status, dry_download), (200, dry_preview))
        status, _, _ = self.request("GET", f"/api/voice/tasks/{task_id}/preview?track=accompaniment", headers=auth)
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", f"/api/voice/tasks/{task_id}/download?track=../dry", headers=auth)
        self.assertEqual(status, 404)

    def test_director_workflow_presets_are_authenticated_safe_and_downloadable(self) -> None:
        status, _, _ = self.request("GET", "/api/workflows/director")
        self.assertEqual(status, 401)
        auth = {"X-API-Key": "test-key"}
        status, _, body = self.request("GET", "/api/workflows/director", headers=auth)
        self.assertEqual(status, 200)
        index = json.loads(body)
        self.assertEqual([item["mode"] for item in index["modes"]], ["r2v", "v2v", "rv2v"])

        status, _, body = self.request(
            "GET", "/api/workflows/director/rv2v", headers=auth,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mode"], "rv2v")

        status, headers, body = self.request(
            "GET", "/api/workflows/director/v2v?download=1", headers=auth,
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        preset = json.loads(body)
        self.assertEqual((preset["kind"], preset["compiler"]), ("template", "h3_ref"))
        self.assertEqual(preset["conditioner_node"], "MiniMaxH3ReferenceToVideo")
        self.assertIn("<Video 1>", preset["video_layout"])
        encoded = body.decode("utf-8")
        self.assertNotIn(str(self.config.data_root), encoded)
        self.assertNotIn(self.config.api_key, encoded)

        status, _, body = self.request(
            "GET", "/api/workflows/director/not-a-path", headers=auth,
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "unknown_workflow_preset")

    def test_exact_compiled_workflow_export_is_job_bound_and_hash_checked(self) -> None:
        job_id = "1" * 32
        workflow = {
            "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"prompt": "<Video 1>"}},
        }
        encoded = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        evidence_root = self.config.data_root / "evidence" / "workflows"
        evidence_root.mkdir(parents=True, exist_ok=True)
        (evidence_root / f"{job_id}.json").write_bytes(encoded)
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "job_id": job_id, "status": "queued",
            "workflow_sha256": digest, "created_at": 1.0, "updated_at": 1.0,
        })
        path = f"/api/jobs/{job_id}/workflow?download=1"
        status, _, _ = self.request("GET", path)
        self.assertEqual(status, 401)
        status, headers, body = self.request("GET", path, headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        self.assertEqual(body, encoded)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

        (evidence_root / f"{job_id}.json").write_bytes(b"{}")
        status, _, body = self.request("GET", path, headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "workflow_integrity")

        status, _, body = self.request(
            "GET", "/api/jobs/not-an-id/workflow", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_id")

    def test_scene_analysis_route_is_authenticated_and_delegates_asset_id(self) -> None:
        asset_id = "a" * 32
        payload = json.dumps({"asset_id": asset_id, "max_cuts": 12}).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        with patch.object(
            self.server.runtime.scene_analysis, "analyze",
            return_value={"asset_id": asset_id, "fps": 24, "frame_count": 120, "cut_frames": []},
        ) as analyze:
            status, _, body = self.request(
                "POST", "/api/media/analyze-scenes", payload, headers,
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["asset_id"], asset_id)
        analyze.assert_called_once_with({"asset_id": asset_id, "max_cuts": 12})

    def test_project_run_route_accepts_only_segment_ids(self) -> None:
        project_id = "b" * 32
        segment_ids = ["c" * 32]
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        with patch.object(
            self.server.runtime.projects, "run",
            return_value={"id": project_id, "status": "running"},
        ) as run:
            payload = json.dumps({"segment_ids": segment_ids}).encode()
            status, _, _ = self.request(
                "POST", f"/api/video-projects/{project_id}/run", payload, headers,
            )
        self.assertEqual(status, 202)
        run.assert_called_once_with(project_id, segment_ids)

        payload = json.dumps({"segment_ids": segment_ids, "unexpected": True}).encode()
        status, _, body = self.request(
            "POST", f"/api/video-projects/{project_id}/run", payload, headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_action")

    def test_asset_library_lists_persisted_reusable_public_contract(self) -> None:
        asset_id = "a" * 32
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id,
            "kind": "image",
            "filename": "reference portrait.png",
            "stored_name": f"{asset_id}.png",
            "comfy_path": f"h3-studio/{asset_id}.png",
            "mime_type": "image/png",
            "size": 1234,
            "media": {"width": 1024, "height": 1024, "codec": "png"},
            "created_at": 123.0,
            "content_url": f"/api/assets/{asset_id}/content",
        })

        status, _, body = self.request(
            "GET", "/api/assets", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        assets = json.loads(body)["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["id"], asset_id)
        self.assertEqual(assets[0]["name"], "reference portrait.png")
        self.assertEqual(assets[0]["type"], "image")
        self.assertEqual(assets[0]["content_url"], f"/api/assets/{asset_id}/content")
        self.assertEqual(assets[0]["media"], {"width": 1024, "height": 1024, "codec": "png"})
        self.assertNotIn("stored_name", assets[0])
        self.assertNotIn("comfy_path", assets[0])

    def test_image_generation_result_echoes_explicit_dimensions(self) -> None:
        request = json.dumps({
            "type": "image", "prompt": "A studio product photograph",
            "width": 2048, "height": 1152,
        }).encode()
        status, _, body = self.request(
            "POST", "/api/generate", request,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202)
        queued = json.loads(body)
        self.assertEqual(
            (queued["parameters"]["width"], queued["parameters"]["height"]),
            (2048, 1152),
        )
        latent = next(
            node for node in self.fake.workflow.values()
            if node.get("class_type") == "EmptySD3LatentImage"
        )
        self.assertEqual(
            (latent["inputs"]["width"], latent["inputs"]["height"]),
            (2048, 1152),
        )

        output = self.config.comfy_output / "generated.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"image-output")
        self.fake.record = {
            "outputs": {"17": {"images": [{"filename": output.name, "subfolder": "", "type": "output"}]}}
        }
        status, _, body = self.request(
            "GET", f"/api/result?id={queued['job_id']}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(
            (result["parameters"]["width"], result["parameters"]["height"]),
            (2048, 1152),
        )
        status, _, body = self.request(
            "GET", f"/api/jobs/{queued['job_id']}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertEqual(detail["raw_prompt"], "A studio product photograph")
        self.assertEqual(detail["prompt_parts"], {})

    def test_prompt_compile_endpoint(self) -> None:
        body = json.dumps({"output_type": "video", "prompt": "A cat walks", "parts": {"camera": "slow dolly in"}}).encode()
        status, _, content = self.request(
            "POST", "/api/prompts/compile", body,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertIn("slow dolly in", json.loads(content)["prompt"])

    def test_v2v_generation_receipt_exposes_source_and_exact_compiler_contract(self) -> None:
        source_id = "2" * 32
        self.server.runtime.assets.metadata.put(source_id, {
            "id": source_id, "kind": "video", "filename": "source.mp4",
            "stored_name": f"{source_id}.mp4", "comfy_path": f"h3-studio/{source_id}.mp4",
            "media": {
                "duration": 5.0, "fps": 24.0, "reference_fps": 24.0,
                "frame_count": 120, "has_audio": True,
            },
            "created_at": 1.0,
        })
        payload = json.dumps({
            "type": "video", "prompt": "Re-stage this shot",
            "prompt_mode": "preserve_tags_only", "director_mode": "v2v",
            "source_asset_id": source_id,
            "references": [{"asset_id": source_id, "role": "motion"}],
        }).encode()
        status, _, body = self.request(
            "POST", "/api/generate", payload,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202, body)
        job = json.loads(body)
        self.assertEqual((job["director_mode"], job["source_asset_id"]), ("v2v", source_id))
        self.assertEqual(job["parameters"]["resolved_director_mode"], "v2v")
        self.assertEqual(job["prompt"], "Re-stage this shot\n\n<Video 1>")
        self.assertEqual(job["references"][0]["asset_id"], source_id)
        self.assertEqual(job["workflow_evidence"]["source_video_tag"], "<Video 1>")
        self.assertEqual(self.fake.workflow["8"]["inputs"]["ref_videos.ref_video_0"], ["101", 0])
        self.assertEqual(self.fake.workflow["8"]["inputs"]["prompt"], job["prompt"])

    def test_video_project_create_list_get_and_put_contract(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va")
        request = {
            "prompt": "A robot walks into the room",
            "parameters": {"duration": 5, "aspect_ratio": "16:9"},
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "references": [],
        }
        payload = json.dumps({"title": "Sequence", "segments": [{"continuation": "none", "request": request}]}).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        status, _, content = self.request("POST", "/api/video-projects", payload, headers)
        self.assertEqual(status, 201)
        created = json.loads(content)
        self.assertEqual(created["current_index"], -1)
        self.assertEqual(created["segments"][0]["status"], "pending")

        status, _, content = self.request("GET", "/api/video-projects", headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["projects"][0]["id"], created["id"])
        status, _, content = self.request("GET", f"/api/video-projects/{created['id']}", headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["title"], "Sequence")

        payload = json.dumps({
            "title": "Revised sequence",
            "segments": [{"id": created["segments"][0]["id"], "continuation": "none", "request": request}],
        }).encode()
        status, _, content = self.request("PUT", f"/api/video-projects/{created['id']}", payload, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["title"], "Revised sequence")

        status, _, content = self.request(
            "POST", f"/api/video-projects/{created['id']}/run", json.dumps({"unexpected": True}).encode(), headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(content)["error"]["code"], "invalid_action")

        status, _, content = self.request(
            "DELETE", f"/api/video-projects/{created['id']}",
            headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["deleted"])

    def test_video_project_http_round_trips_storyboard_without_storage_paths(self) -> None:
        asset_id = "7" * 32
        stored_name = f"{asset_id}.mp4"
        path = self.server.runtime.assets.upload_root / stored_name
        path.write_bytes(b"video")
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": "video", "filename": "source.mp4",
            "stored_name": stored_name, "comfy_path": f"h3-studio/{stored_name}",
            "media": {"fps": 24.0, "frame_count": 480, "duration": 20.0},
            "created_at": 1.0,
        })
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va")
        generation = {
            "prompt": "A source shot is reimagined",
            "parameters": {"duration": 5, "aspect_ratio": "16:9"},
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(), "references": [],
        }
        storyboard = {
            "source_asset_id": asset_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [120, 240],
        }
        source_range = {
            "asset_id": asset_id, "start_frame": 120,
            "end_frame": 360, "fps": 24.0,
        }
        payload = json.dumps({
            "title": "HTTP storyboard", "storyboard": storyboard,
            "segments": [{
                "continuation": "none", "request": generation,
                "source_range": source_range,
            }],
        }).encode()
        status, _, content = self.request(
            "POST", "/api/video-projects", payload,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 201)
        receipt = json.loads(content)
        self.assertEqual(receipt["storyboard"], storyboard)
        self.assertEqual(receipt["segments"][0]["source_range"], source_range)
        encoded = content.decode()
        self.assertNotIn("stored_name", encoded)
        self.assertNotIn("comfy_path", encoded)
        self.assertNotIn(str(self.config.comfy_input), encoded)

    def test_video_project_http_boundary_accepts_1000_and_rejects_1001(self) -> None:
        profile = DEFAULT_REGISTRY.get("minimax-h3-fl2va-base")
        request = {
            "prompt": ("cinematic subject action scene camera lighting continuity " * 10).strip(),
            "parameters": {"duration": 5, "aspect_ratio": "16:9", "steps": 20},
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "references": [],
        }
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        segments = [{"continuation": "none", "request": request} for _ in range(1000)]
        payload = json.dumps({"title": "1000 segment boundary", "segments": segments}, separators=(",", ":")).encode()
        self.assertGreater(len(payload), self.config.max_json_bytes)
        self.assertLess(len(payload), self.config.max_project_json_bytes)
        status, _, content = self.request(
            "POST", "/api/video-projects", payload, headers, timeout=15,
        )
        self.assertEqual(status, 201, content[:500])
        self.assertEqual(len(json.loads(content)["segments"]), 1000)

        payload = json.dumps({"title": "1001 rejected", "segments": [*segments, segments[0]]}, separators=(",", ":")).encode()
        status, _, content = self.request("POST", "/api/video-projects", payload, headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(content)["error"]["code"], "invalid_segments")

    def test_comfy_not_found_becomes_persisted_failure(self) -> None:
        request = json.dumps({"type": "video", "prompt": "A cloud"}).encode()
        status, _, content = self.request(
            "POST", "/api/generate", request,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202)
        job_id = json.loads(content)["job_id"]
        self.fake.status_value = "not_found"
        status, _, content = self.request("GET", f"/api/status?id={job_id}", headers={"X-API-Key": "test-key"})
        result = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["progress"], 100)
        self.assertEqual(self.server.runtime.jobs.get(job_id)["status"], "failed")

    def test_upload_generate_status_and_download(self) -> None:
        boundary = "----api-integration"
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        upload_body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"frame.png\"\r\n"
            "Content-Type: image/png\r\n\r\n"
        ).encode() + png + f"\r\n--{boundary}--\r\n".encode()
        status, headers, body = self.request(
            "POST",
            "/api/assets",
            upload_body,
            {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-API-Key": "test-key",
                "Origin": "http://localhost:3000",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:3000")
        uploaded = json.loads(body)
        self.assertEqual(uploaded["kind"], "image")
        self.assertEqual(len(uploaded["asset_id"]), 32)
        self.assertEqual(uploaded["sha256"], hashlib.sha256(png).hexdigest())
        self.assertFalse(uploaded["reused"])

        status, _, body = self.request(
            "POST",
            "/api/assets",
            upload_body,
            {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-API-Key": "test-key",
            },
        )
        duplicate = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["asset_id"], uploaded["asset_id"])
        self.assertEqual(len(self.server.runtime.assets.list_public()), 1)

        generate_body = json.dumps(
            {
                "type": "video",
                "prompt": "A cinematic sunrise",
                "aspectRatio": "16:9",
                "duration": 5,
                "steps": 4,
            }
        ).encode()
        status, _, body = self.request(
            "POST",
            "/api/generate",
            generate_body,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202)
        job = json.loads(body)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["director_mode"], "t2v")
        self.assertIsNone(job["source_asset_id"])
        self.assertEqual(job["parameters"]["resolved_director_mode"], "t2v")
        self.assertNotIn("result_url", job)
        self.assertIsNotNone(self.fake.workflow)
        job_id = job["job_id"]
        evidence = job["workflow_evidence"]
        evidence_path = self.config.data_root / evidence["path"]
        self.assertTrue(evidence_path.is_file())
        self.assertEqual(hashlib.sha256(evidence_path.read_bytes()).hexdigest(), evidence["sha256"])
        self.assertEqual(evidence["steps"], 4)
        self.assertEqual(evidence["sampler"], "sa_solver")
        self.assertEqual(evidence["scheduler"], "simple")
        self.assertEqual(evidence["lora"], "fl-lora.safetensors")
        self.assertEqual(evidence["seed"], job["parameters"]["seed"])
        self.assertEqual((evidence["width"], evidence["height"]), (1344, 768))
        self.assertEqual(evidence["frames"], job["parameters"]["frames"])
        self.assertEqual(evidence["resolved_director_mode"], "t2v")

        status, _, body = self.request(
            "GET", f"/api/status?id={job_id}", headers={"X-API-Key": "test-key"}
        )
        self.assertEqual(status, 200)
        hydrated = json.loads(body)
        self.assertIsInstance(hydrated["created_at"], (int, float))
        self.assertIsInstance(hydrated["updated_at"], (int, float))
        queued = json.loads(body)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["workflow_evidence"]["sha256"], evidence["sha256"])

        output = self.config.comfy_output / "test.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video-bytes")
        self.fake.record = {
            "outputs": {
                "17": {
                    "images": [{"filename": "test.mp4", "subfolder": "", "type": "output"}]
                }
            }
        }
        status, _, body = self.request(
            "GET", f"/api/result?id={job_id}", headers={"X-API-Key": "test-key"}
        )
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["status"], "completed")
        self.assertIn("download_url", result)
        self.assertEqual(result["workflow_evidence"]["sampler"], "sa_solver")
        self.assertEqual(result["workflow_evidence"]["sha256"], result["workflow_sha256"])
        self.assertIn("preview_url", result)
        self.assertEqual(result["progress"], 100)

        status, headers, body = self.request(
            "GET", f"/api/download?id={job_id}", headers={"X-API-Key": "test-key"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(body, b"video-bytes")

        status, headers, body = self.request(
            "GET", f"/api/preview?id={job_id}", headers={"X-API-Key": "test-key", "Range": "bytes=1-5"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 1-5/11")
        self.assertEqual(body, b"ideo-")

    def test_result_summary_list_is_small_filtered_and_conditionally_cached(self) -> None:
        completed_id = "7" * 32
        failed_id = "8" * 32
        self.server.runtime.jobs.put(completed_id, {
            "id": completed_id, "status": "completed", "output_type": "video",
            "created_at": 10, "updated_at": 11,
            "prompt": "x" * 20_000, "raw_prompt": "y" * 20_000,
            "outputs": [{"filename": "result.mp4", "subfolder": "", "type": "output"}],
        })
        self.server.runtime.jobs.put(failed_id, {
            "id": failed_id, "status": "failed", "output_type": "video",
            "created_at": 12, "updated_at": 12,
            "prompt": "must not appear", "outputs": [],
        })

        status, headers, body = self.request(
            "GET", "/api/jobs?limit=20&summary=1&results=1", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual([item["id"] for item in listing["jobs"]], [completed_id])
        self.assertEqual(listing["instance_id"], self.server.runtime.instance_id)
        self.assertEqual(listing["jobs"][0]["prompt"], "x" * 512)
        self.assertNotIn("raw_prompt", listing["jobs"][0])
        self.assertLess(len(body), 3_000)
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        self.assertTrue(headers["ETag"].startswith('"'))

        status, cached_headers, cached_body = self.request(
            "GET", "/api/jobs?limit=20&summary=1&results=1",
            headers={"X-API-Key": "test-key", "If-None-Match": headers["ETag"]},
        )
        self.assertEqual(status, 304)
        self.assertEqual(cached_headers["ETag"], headers["ETag"])
        self.assertEqual(cached_body, b"")

    def test_completed_result_pin_is_persisted_and_returned_outside_first_page(self) -> None:
        pinned_id = "a" * 32
        newest_id = "b" * 32
        for job_id, created_at in ((pinned_id, 1), (newest_id, 2)):
            self.server.runtime.jobs.put(job_id, {
                "id": job_id, "status": "completed", "output_type": "video",
                "outputs": [{"filename": f"{job_id}.mp4", "subfolder": "", "type": "output"}],
                "created_at": created_at, "updated_at": created_at,
            })
        headers = {"X-API-Key": "test-key", "Content-Type": "application/json"}
        status, _, body = self.request("PATCH", f"/api/jobs/{pinned_id}", json.dumps({"pinned": True}).encode(), headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["pinned"])

        status, _, body = self.request(
            "GET", "/api/jobs?limit=1&summary=1&results=1&include_pinned=1",
            headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200, body)
        listing = json.loads(body)
        self.assertEqual([item["id"] for item in listing["jobs"]], [newest_id])
        self.assertEqual([item["id"] for item in listing["pinned_jobs"]], [pinned_id])
        self.assertTrue(self.server.runtime.jobs.get(pinned_id)["pinned"])

        status, _, body = self.request("PATCH", f"/api/jobs/{pinned_id}", json.dumps({"pinned": "yes"}).encode(), headers)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_parameter")

    def test_media_supports_head_validators_resume_and_standard_range_errors(self) -> None:
        job_id = "9" * 32
        output = self.config.comfy_output / "cached.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"0123456789")
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "status": "completed", "output_type": "video",
            "created_at": 1, "updated_at": 2,
            "outputs": [{"filename": output.name, "subfolder": "", "type": "output"}],
        })
        auth = {"X-API-Key": "test-key"}

        status, headers, body = self.request("HEAD", f"/api/preview?id={job_id}", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Content-Length"], "10")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertIn("immutable", headers["Cache-Control"])
        etag = headers["ETag"]

        status, _, body = self.request(
            "GET", f"/api/preview?id={job_id}", headers={**auth, "If-None-Match": etag},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        status, headers, body = self.request(
            "GET", f"/api/preview?id={job_id}", headers={**auth, "Range": "bytes=3-6", "If-Range": etag},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 3-6/10")
        self.assertEqual(body, b"3456")
        status, headers, body = self.request(
            "GET", f"/api/preview?id={job_id}", headers={**auth, "Range": "bytes=99-100"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */10")
        self.assertEqual(body, b"")

    def test_head_does_not_suppress_the_next_get_on_a_persistent_connection(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"X-API-Key": "test-key"}
        try:
            connection.request("HEAD", "/api/assets", headers=headers)
            head = connection.getresponse()
            self.assertEqual(head.status, 200)
            self.assertEqual(head.read(), b"")
            connection.request("GET", "/api/assets", headers=headers)
            get = connection.getresponse()
            self.assertEqual(get.status, 200)
            self.assertEqual(json.loads(get.read()), {"assets": []})
        finally:
            connection.close()

    def test_result_summary_infers_legacy_visual_output_type(self) -> None:
        legacy_id = "6" * 32
        output = self.config.comfy_output / "legacy.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"legacy-video")
        self.server.runtime.jobs.put(legacy_id, {
            "id": legacy_id, "status": "completed", "created_at": 9,
            "outputs": [{"filename": "legacy.mp4", "subfolder": "", "type": "output"}],
        })
        status, _, body = self.request(
            "GET", "/api/jobs?limit=20&summary=1&results=1", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        item = json.loads(body)["jobs"][0]
        self.assertEqual(item["id"], legacy_id)
        self.assertEqual(item["output_type"], "video")
        self.assertIn("thumbnail_url", item)
        thumbnail = self.config.data_root / "cache" / "legacy.jpg"
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        thumbnail.write_bytes(b"jpeg-thumbnail")
        with patch.object(self.server.runtime.media, "thumbnail", return_value=thumbnail):
            status, _, thumbnail_body = self.request(
                "GET", item["thumbnail_url"], headers={"X-API-Key": "test-key"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(thumbnail_body, b"jpeg-thumbnail")

    def test_completed_result_can_be_persisted_once_and_reused_by_canvas_nodes(self) -> None:
        job_id = "d" * 32
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        output = self.config.comfy_output / "generated-result.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        self.server.runtime.jobs.put(job_id, {
            "id": job_id,
            "job_id": job_id,
            "status": "completed",
            "output_type": "image",
            "outputs": [{
                "filename": output.name,
                "subfolder": "",
                "type": "output",
                "mime_type": "image/png",
            }],
            "created_at": 1.0,
            "updated_at": 2.0,
        })
        payload = json.dumps({"index": 0}).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}

        first_status, _, first_body = self.request("POST", f"/api/jobs/{job_id}/assets", payload, headers)
        second_status, _, second_body = self.request("POST", f"/api/jobs/{job_id}/assets", payload, headers)
        first, second = json.loads(first_body), json.loads(second_body)

        self.assertEqual((first_status, second_status), (201, 200))
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertEqual(first["asset"]["kind"], "image")
        self.assertEqual(len(self.server.runtime.assets.list()), 1)
        self.assertEqual(self.server.runtime.jobs.get(job_id)["outputs"][0]["asset_id"], first["asset_id"])
        self.assertEqual(self.server.runtime.jobs.get(job_id)["updated_at"], 2.0)

    def test_job_result_can_be_materialized_internal_then_promoted_without_changing_asset_id(self) -> None:
        job_id = "1" * 32
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        output = self.config.comfy_output / "internal-upstream.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "job_id": job_id, "status": "completed", "output_type": "image",
            "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "mime_type": "image/png"}],
            "created_at": 1.0, "updated_at": 2.0,
        })
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}

        status, _, body = self.request(
            "POST", f"/api/jobs/{job_id}/assets",
            json.dumps({"index": 0, "visibility": "internal"}).encode(), headers,
        )
        internal = json.loads(body)
        self.assertEqual(status, 201)
        self.assertEqual(internal["asset"]["visibility"], "internal")
        self.assertEqual(self.server.runtime.assets.get(internal["asset_id"])["visibility"], "internal")

        status, _, body = self.request("GET", "/api/assets", headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assets"], [])

        profile = DEFAULT_REGISTRY.get("flux2-klein-4b-fp8")
        compile_body = {
            "output_type": "image", "prompt": "Use the connected image",
            "references": [{"asset_id": internal["asset_id"], "role": "reference"}],
            "profile_id": profile.id, "profile_version": profile.version,
            "profile_digest": profile.digest(),
        }
        status, _, body = self.request(
            "POST", "/api/prompts/compile", json.dumps(compile_body).encode(), headers,
        )
        self.assertEqual(status, 200, body.decode())

        status, _, body = self.request(
            "POST", f"/api/jobs/{job_id}/assets", json.dumps({"index": 0}).encode(), headers,
        )
        promoted = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(promoted["reused"])
        self.assertEqual(promoted["asset_id"], internal["asset_id"])
        self.assertEqual(promoted["asset"]["visibility"], "library")
        self.assertEqual(self.server.runtime.jobs.get(job_id)["outputs"][0]["asset_visibility"], "library")

        status, _, body = self.request("GET", "/api/assets", headers={"X-API-Key": "test-key"})
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["assets"]], [internal["asset_id"]])

    def test_completed_internal_materialization_is_reclaimable_by_gc(self) -> None:
        job_id = "2" * 32
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        output = self.config.comfy_output / "gc-internal.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "job_id": job_id, "status": "completed", "output_type": "image",
            "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "mime_type": "image/png"}],
            "created_at": 1.0, "updated_at": 2.0,
        })
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        status, _, body = self.request(
            "POST", f"/api/jobs/{job_id}/assets",
            json.dumps({"index": 0, "visibility": "internal"}).encode(), headers,
        )
        asset_id = json.loads(body)["asset_id"]
        self.assertEqual(status, 201)
        asset = self.server.runtime.assets.get(asset_id)
        asset["created_at"] = 1.0
        self.server.runtime.assets.metadata.put(asset_id, asset)

        consumer_id = "3" * 32
        consumer = {
            "id": consumer_id, "status": "running",
            "references": [{"asset_id": asset_id, "role": "reference"}],
            "created_at": 3.0, "updated_at": 3.0,
        }
        self.server.runtime.jobs.put(consumer_id, consumer)
        status, _, body = self.request(
            "POST", "/api/maintenance/gc",
            json.dumps({"dry_run": False, "older_than_days": 1}).encode(), headers,
        )
        self.assertEqual(status, 200)
        self.assertNotIn(asset_id, json.loads(body)["asset_ids"])
        self.assertEqual(self.server.runtime.assets.get(asset_id)["id"], asset_id)

        consumer["status"] = "completed"
        self.server.runtime.jobs.put(consumer_id, consumer)

        status, _, body = self.request(
            "POST", "/api/maintenance/gc",
            json.dumps({"dry_run": False, "older_than_days": 1}).encode(), headers,
        )
        self.assertEqual(status, 200)
        self.assertIn(asset_id, json.loads(body)["asset_ids"])
        with self.assertRaises(ApiError):
            self.server.runtime.assets.get(asset_id)

    def test_generate_request_id_is_idempotent(self) -> None:
        request_body = json.dumps(
            {"request_id": "e" * 32, "type": "video", "prompt": "A cloud drifts"}
        ).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        first_status, _, first_body = self.request("POST", "/api/generate", request_body, headers)
        second_status, _, second_body = self.request("POST", "/api/generate", request_body, headers)
        first, second = json.loads(first_body), json.loads(second_body)
        self.assertEqual((first_status, second_status), (202, 202))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.fake.submit_count, 1)

    def test_submission_failure_is_persisted_and_same_request_replays_failed_job(self) -> None:
        request_id = "9" * 32
        request_body = json.dumps(
            {"request_id": request_id, "type": "video", "prompt": "Persist failure"}
        ).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        with patch.object(
            self.fake, "ensure_capability",
            side_effect=ApiError(503, "comfy_unavailable", "temporary backend failure"),
        ):
            first_status, _, first_body = self.request("POST", "/api/generate", request_body, headers)
        self.assertEqual(first_status, 503, first_body)
        failed = next(job for job in self.server.runtime.jobs.list() if job["request_id"] == request_id)
        self.assertEqual((failed["status"], failed["error_code"]), ("failed", "comfy_unavailable"))

        replay_status, _, replay_body = self.request("POST", "/api/generate", request_body, headers)
        replay = json.loads(replay_body)
        self.assertEqual(replay_status, 202, replay_body)
        self.assertEqual(replay["job_id"], failed["job_id"])
        self.assertEqual(replay["status"], "failed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len([job for job in self.server.runtime.jobs.list() if job["request_id"] == request_id]), 1)

    def test_request_id_reuse_with_different_payload_is_a_conflict(self) -> None:
        headers = {"Content-Type": "application/json", "X-API-Key": "test-key"}
        first = json.dumps(
            {"request_id": "d" * 32, "type": "video", "prompt": "First operation"}
        ).encode()
        changed = json.dumps(
            {"request_id": "d" * 32, "type": "video", "prompt": "Different operation"}
        ).encode()
        first_status, _, _ = self.request("POST", "/api/generate", first, headers)
        changed_status, _, changed_body = self.request("POST", "/api/generate", changed, headers)
        self.assertEqual(first_status, 202)
        self.assertEqual(changed_status, 409)
        self.assertEqual(json.loads(changed_body)["error"]["code"], "idempotency_conflict")
        self.assertEqual(self.fake.submit_count, 1)

    def test_cancel_is_persisted_and_terminal_replay_does_not_cancel_again(self) -> None:
        body = json.dumps({"request_id": "c" * 32, "type": "video", "prompt": "A cloud"}).encode()
        status, _, content = self.request(
            "POST", "/api/generate", body,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202)
        job_id = json.loads(content)["job_id"]
        cancel_path = f"/api/jobs/{job_id}/cancel"
        status, _, content = self.request(
            "POST", cancel_path, b"{}",
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["status"], "canceled")
        self.assertEqual(self.fake.canceled, [self.fake.prompt_id])
        self.assertEqual(self.server.runtime.jobs.get(job_id)["status"], "canceled")

        status, _, content = self.request(
            "POST", cancel_path, b"{}",
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["already_terminal"])
        self.assertEqual(self.fake.canceled, [self.fake.prompt_id])

    def test_cancel_consumes_declared_body_and_keeps_connection_usable(self) -> None:
        body = json.dumps({"request_id": "b" * 32, "type": "video", "prompt": "A cloud"}).encode()
        status, _, content = self.request(
            "POST", "/api/generate", body,
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 202)
        job_id = json.loads(content)["job_id"]

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST", f"/api/jobs/{job_id}/cancel", body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "2", "X-API-Key": "test-key"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        connection.request("GET", "/api/health")
        health = connection.getresponse()
        health.read()
        connection.close()
        self.assertEqual(health.status, 200)

    def test_asset_delete_is_blocked_while_saved_job_references_it(self) -> None:
        asset_id = "f" * 32
        stored_name = f"{asset_id}.png"
        path = self.server.runtime.assets.upload_root / stored_name
        path.write_bytes(b"asset")
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id,
            "kind": "image",
            "filename": "frame.png",
            "stored_name": stored_name,
            "comfy_path": f"h3-studio/{stored_name}",
            "size": 5,
            "created_at": 1,
        })
        job_id = "e" * 32
        self.server.runtime.jobs.put(job_id, {
            "id": job_id,
            "status": "failed",
            "references": [{"asset_id": asset_id}],
            "created_at": 1,
        })

        status, _, content = self.request(
            "DELETE", f"/api/assets/{asset_id}", headers={"X-API-Key": "test-key"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(content)["error"]["code"], "asset_in_use")
        self.assertTrue(path.is_file())

        self.server.runtime.jobs.delete(job_id)
        status, _, content = self.request(
            "DELETE", f"/api/assets/{asset_id}", headers={"X-API-Key": "test-key"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(content)["deleted"])
        self.assertFalse(path.exists())

    def test_voice_task_references_assets_until_task_is_deleted(self) -> None:
        asset_id = "0" * 32
        stored_name = f"{asset_id}.wav"
        path = self.server.runtime.assets.upload_root / stored_name
        path.write_bytes(b"RIFFasset")
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": "audio", "filename": "voice.wav",
            "stored_name": stored_name, "comfy_path": f"h3-studio/{stored_name}",
            "size": path.stat().st_size, "created_at": 1,
        })
        task_id = "1" * 32
        self.server.runtime.voice.store.put(task_id, {
            "id": task_id, "task_id": task_id, "engine": "vevo2",
            "source_asset_id": asset_id, "reference_asset_id": asset_id,
            "status": "failed", "created_at": 1, "updated_at": 1,
        })
        auth = {"X-API-Key": "test-key"}
        status, _, body = self.request("DELETE", f"/api/assets/{asset_id}", headers=auth)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "asset_in_use")
        status, _, body = self.request("DELETE", f"/api/voice/tasks/{task_id}", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["deleted"])
        status, _, _ = self.request("DELETE", f"/api/assets/{asset_id}", headers=auth)
        self.assertEqual(status, 200)

    def test_asset_delete_is_blocked_while_storyboard_source_range_references_it(self) -> None:
        asset_id = "9" * 32
        stored_name = f"{asset_id}.mp4"
        path = self.server.runtime.assets.upload_root / stored_name
        path.write_bytes(b"video")
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": "video", "filename": "story-source.mp4",
            "stored_name": stored_name, "comfy_path": f"h3-studio/{stored_name}",
            "size": 5, "media": {"fps": 24.0, "frame_count": 240, "duration": 10.0},
            "created_at": 1,
        })
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va")
        project = self.server.runtime.projects.create({
            "title": "Protected storyboard source",
            "storyboard": {
                "source_asset_id": asset_id, "fps": 24.0,
                "frame_count": 240, "cut_frames": [],
            },
            "segments": [{
                "continuation": "none",
                "source_range": {
                    "asset_id": asset_id, "start_frame": 0,
                    "end_frame": 240, "fps": 24.0,
                },
                "request": {
                    "prompt": "Reframe the protected source",
                    "parameters": {"duration": 5, "aspect_ratio": "16:9"},
                    "profile_id": profile.id, "profile_version": profile.version,
                    "profile_digest": profile.digest(), "references": [],
                },
            }],
        })

        status, _, content = self.request(
            "DELETE", f"/api/assets/{asset_id}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(content)["error"]["code"], "asset_in_use")
        self.assertTrue(path.is_file())

        status, _, content = self.request(
            "POST", "/api/maintenance/gc",
            json.dumps({"dry_run": False, "older_than_days": 1}).encode(),
            {"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(asset_id, json.loads(content)["asset_ids"])
        self.assertTrue(path.is_file())

        self.server.runtime.projects.delete(project["id"])
        status, _, content = self.request(
            "DELETE", f"/api/assets/{asset_id}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(path.exists())

    def test_completed_job_delete_removes_unique_output_but_preserves_saved_asset(self) -> None:
        job_id = "7" * 32
        asset_id = "8" * 32
        self.config.comfy_output.mkdir(parents=True, exist_ok=True)
        output_path = self.config.comfy_output / "delete-me.mp4"
        output_path.write_bytes(b"generated-video")
        stored_name = f"{asset_id}.mp4"
        asset_path = self.server.runtime.assets.upload_root / stored_name
        asset_path.write_bytes(b"saved-copy")
        self.server.runtime.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": "video", "filename": "saved-copy.mp4",
            "stored_name": stored_name, "comfy_path": f"h3-studio/{stored_name}",
            "size": 10, "created_at": 1,
        })
        self.server.runtime.jobs.put(job_id, {
            "id": job_id, "status": "completed", "created_at": 1,
            "outputs": [{
                "filename": output_path.name, "subfolder": "", "type": "output",
                "asset_id": asset_id,
            }],
        })

        status, _, content = self.request(
            "DELETE", f"/api/jobs/{job_id}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 200)
        receipt = json.loads(content)
        self.assertTrue(receipt["deleted"])
        self.assertEqual(receipt["outputs_deleted"], 1)
        self.assertEqual(receipt["saved_asset_ids_preserved"], [asset_id])
        self.assertFalse(output_path.exists())
        self.assertTrue(asset_path.is_file())
        self.assertEqual(self.server.runtime.assets.get(asset_id)["id"], asset_id)
        with self.assertRaises(ApiError):
            self.server.runtime.jobs.get(job_id)

    def test_active_or_project_referenced_job_cannot_be_deleted(self) -> None:
        active_id = "6" * 32
        self.server.runtime.jobs.put(active_id, {
            "id": active_id, "status": "running", "created_at": 1, "outputs": [],
        })
        status, _, content = self.request(
            "DELETE", f"/api/jobs/{active_id}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(content)["error"]["code"], "job_busy")
        self.assertEqual(self.server.runtime.jobs.get(active_id)["status"], "running")

        referenced_id = "5" * 32
        project_id = "4" * 32
        self.server.runtime.jobs.put(referenced_id, {
            "id": referenced_id, "status": "completed", "created_at": 2, "outputs": [],
        })
        self.server.runtime.projects.store.put(project_id, {
            "id": project_id, "title": "references result", "status": "draft", "created_at": 2,
            "segments": [{"id": "segment-1", "result_job_id": referenced_id}],
        })
        status, _, content = self.request(
            "DELETE", f"/api/jobs/{referenced_id}", headers={"X-API-Key": "test-key"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(content)["error"]["code"], "job_in_use")
        self.assertEqual(self.server.runtime.jobs.get(referenced_id)["id"], referenced_id)


if __name__ == "__main__":
    unittest.main()
