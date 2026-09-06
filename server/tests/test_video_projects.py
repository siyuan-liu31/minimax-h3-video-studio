from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import random
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.character_migration import SCHEMA_VERSION as MIGRATION_VERSION, plan as plan_migration
from server.errors import ApiError
from server.profiles import DEFAULT_REGISTRY, H3_MAX_DURATION_SECONDS
from server.security import secure_join
from server.storage import AssetStore, JobStore
from server.tests.test_workflows import config
from server.video_projects import VideoProjectManager
from server.workflows import compile_video_workflow, parse_generation_request


MEDIA = {
    "duration": 5.167, "has_video": True, "has_audio": True,
    "video_codec": "h264", "audio_codec": "aac", "width": 1344,
    "height": 768, "fps": 24.0, "frame_count": 124,
}
SILENT_DERIVED_MEDIA = {
    **MEDIA, "has_audio": False, "audio_codec": None,
}
REAL_PROBE_MEDIA = AssetStore._probe_media


class FakeComfy:
    def __init__(self, output_root: Path, *, complete: bool = True) -> None:
        self.output_root = output_root
        self.complete = complete
        self.submit_count = 0
        self.canceled: list[str] = []
        self.records: dict[str, dict] = {}
        self.workflows: list[dict] = []

    def ensure_capability(self, *_args):
        return None

    def ensure_motion_context(self, *_args):
        return None

    def submit(self, workflow, _client_id):
        self.submit_count += 1
        self.workflows.append(workflow)
        prompt_id = f"prompt-{self.submit_count}"
        output = self.output_root / f"clip-{self.submit_count}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\x00\x00\x00\x18ftypisom" + prompt_id.encode())
        self.records[prompt_id] = {
            "outputs": {"20": {"videos": [{"filename": output.name, "subfolder": "", "type": "output"}]}},
        }
        if "19" in workflow:
            latent = self.output_root / f"clip-{self.submit_count}.latent"
            latent.write_bytes(f"latent-{prompt_id}".encode())
            self.records[prompt_id]["outputs"]["19"] = {
                "latents": [{"filename": latent.name, "subfolder": "", "type": "output"}],
            }
        return prompt_id

    def status(self, prompt_id):
        if self.complete:
            return {"status": "completed", "record": self.records[prompt_id]}
        return {"status": "queued"}

    def cancel(self, prompt_id):
        self.canceled.append(prompt_id)

    def find_prompt_by_client_id(self, _client_id):
        return None


def wait_until(predicate, timeout: float = 3) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


class VideoProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = config(self.root)
        self.settings.prepare()
        self.assets = AssetStore(self.settings)
        self.jobs = JobStore(self.settings.data_root / "metadata" / "jobs")
        self.comfy = FakeComfy(self.settings.comfy_output)
        # Continuation/source-range imports are all video-only ffmpeg products.
        self.probe = patch.object(AssetStore, "_probe_media", return_value=dict(SILENT_DERIVED_MEDIA))
        self.probe_image = patch.object(AssetStore, "_probe_image", return_value={"width": 1344, "height": 768, "codec": "png"})
        self.probe.start()
        self.probe_image.start()
        self.manager = VideoProjectManager(
            self.settings, self.assets, self.jobs, self.comfy, DEFAULT_REGISTRY, threading.RLock(),
        )

    def tearDown(self) -> None:
        self.probe.stop()
        self.probe_image.stop()
        self.temp.cleanup()

    @staticmethod
    def request(profile_id: str = "minimax-h3-fl2va") -> dict:
        profile = DEFAULT_REGISTRY.get(profile_id)
        return {
            "prompt": "A cream robot continues walking through a living room",
            "parts": {"camera": "steady medium tracking shot"},
            "parameters": {"duration": 5, "aspect_ratio": "16:9"},
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "references": [],
        }

    def add_asset(self, asset_id: str, kind: str, role_media: dict | None = None) -> None:
        suffix = {"image": ".png", "video": ".mp4", "audio": ".wav"}[kind]
        name = f"{asset_id}{suffix}"
        path = self.settings.comfy_input / "h3-studio" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
        self.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": kind, "filename": name, "stored_name": name,
            "comfy_path": f"h3-studio/{name}", "media": role_media or {}, "created_at": time.time(),
        })

    def create_project(self, continuations: list[str], requests: list[dict] | None = None) -> dict:
        requests = requests or [self.request() for _ in continuations]
        return self.manager.create({
            "title": "Long sequence",
            "segments": [
                {"continuation": continuation, "request": requests[index]}
                for index, continuation in enumerate(continuations)
            ],
        })

    def stub_video_only_ffmpeg(self) -> None:
        def command_runner(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomsilent-derived")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = command_runner

    def test_direct_media_clip_is_persisted_and_merged_without_h3_submission(self) -> None:
        asset_id = "f" * 32
        self.add_asset(asset_id, "video", dict(MEDIA))
        metadata = self.assets.metadata.get(asset_id)
        source = self.assets.content_path(metadata)
        metadata["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assets.metadata.put(asset_id, metadata)
        created = self.manager.create({
            "title": "Imported clip",
            "segments": [{
                "kind": "media",
                "media_source": {
                    "type": "asset", "asset_id": asset_id,
                    "start_frame": 12, "end_frame": 108, "fps": 24.0,
                    "keep_audio": True,
                },
            }],
        })
        self.assertEqual(created["status"], "completed")
        self.assertEqual(created["segments"][0]["kind"], "media")
        self.assertNotIn("request", created["segments"][0])
        def command_runner(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomdirect-merged")
            return subprocess.CompletedProcess(command, 0, "", "")
        self.manager.command_runner = command_runner
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] in {"completed", "failed"})
        receipt = self.manager.get(created["id"])
        self.assertEqual(receipt["status"], "completed", receipt.get("merged"))
        self.assertEqual(self.comfy.submit_count, 0)
        self.assertEqual(receipt["merged"]["sources"][0]["source_type"], "asset")

    def test_generation_continuation_rejects_a_direct_media_predecessor(self) -> None:
        asset_id = "e" * 32
        self.add_asset(asset_id, "video", dict(MEDIA))
        with self.assertRaises(ApiError) as raised:
            self.manager.create({
                "title": "Invalid continuation",
                "segments": [
                    {"kind": "media", "media_source": {"type": "asset", "asset_id": asset_id, "start_frame": 0, "end_frame": 124, "fps": 24.0, "keep_audio": True}},
                    {"continuation": "previous_video", "request": self.request("minimax-h3-ref2va")},
                ],
            })
        self.assertEqual(raised.exception.code, "continuation_from_media")

    def test_motion_context_three_segment_turbo_chain_uses_latents_trim_and_custom_settings(self) -> None:
        requests = [self.request() for _ in range(3)]
        for request in requests:
            request["parameters"]["steps"] = 4
        created = self.manager.create({
            "title": "Three panel continuity",
            "segments": [
                {"continuation": "none", "request": requests[0]},
                {"continuation": "motion_context", "motion_context": {"video_frames": 5, "audio_frames": 24}, "request": requests[1]},
                {"continuation": "motion_context", "motion_context": {"video_frames": 22, "audio_frames": 48}, "request": requests[2]},
            ],
        })
        self.manager.run(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] in {"completed", "failed"})
        receipt = self.manager.get(created["id"])
        self.assertEqual(receipt["status"], "completed", receipt)
        self.assertEqual(len(self.comfy.workflows), 3)
        self.assertIn("19", self.comfy.workflows[0])
        self.assertNotIn("22", self.comfy.workflows[0])
        self.assertEqual(self.comfy.workflows[1]["22"]["inputs"]["context_length"], "5")
        self.assertEqual(self.comfy.workflows[2]["22"]["inputs"]["context_length"], "22")
        self.assertEqual(self.comfy.workflows[2]["22"]["inputs"]["audio_context_length"], 48)
        self.assertEqual(self.comfy.workflows[1]["16"]["inputs"]["images"], ["23", 0])
        self.assertEqual([workflow["12"]["inputs"]["steps"] for workflow in self.comfy.workflows], [4, 4, 4])
        self.assertTrue(receipt["segments"][0]["attempts"][-1]["motion_context_state"]["available"])
        self.assertTrue(receipt["segments"][1]["attempts"][-1]["motion_context_state"]["available"])

    def test_motion_context_save_failure_keeps_primary_video_and_blocks_only_next_link(self) -> None:
        created = self.manager.create({
            "title": "Primary output survives context failure",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {"continuation": "motion_context", "request": self.request()},
            ],
        })
        with patch.object(
            self.manager.motion_contexts,
            "capture",
            side_effect=ApiError(507, "motion_context_storage_full", "context quota is full"),
        ):
            self.manager.run(created["id"])
            wait_until(lambda: self.manager.get(created["id"])["status"] in {"completed", "failed"})
        receipt = self.manager.get(created["id"])
        first = receipt["segments"][0]
        second = receipt["segments"][1]
        self.assertEqual(first["status"], "completed")
        self.assertFalse(first["attempts"][-1]["motion_context_state"]["available"])
        self.assertEqual(second["status"], "failed")
        self.assertIn("no usable Motion Context latent", second["error"])
        source_job = self.jobs.get(first["job_id"])
        self.assertEqual(source_job["status"], "completed")
        self.assertTrue(source_job["outputs"])
        self.assertTrue((self.settings.comfy_output / source_job["outputs"][0]["filename"]).is_file())

    def test_motion_context_validation_rejects_bad_windows_first_frame_and_resolution_drift(self) -> None:
        with self.assertRaises(ApiError) as bad_window:
            self.manager.create({
                "title": "bad",
                "segments": [
                    {"continuation": "none", "request": self.request()},
                    {"continuation": "motion_context", "motion_context": {"video_frames": 6}, "request": self.request()},
                ],
            })
        self.assertEqual(bad_window.exception.code, "invalid_motion_context")

        image_id = "9" * 32
        self.add_asset(image_id, "image")
        conflicted = self.request()
        conflicted["references"] = [{"asset_id": image_id, "role": "first_frame"}]
        with self.assertRaises(ApiError) as first_frame:
            self.manager.create({
                "title": "conflict",
                "segments": [
                    {"continuation": "none", "request": self.request()},
                    {"continuation": "motion_context", "request": conflicted},
                ],
            })
        self.assertEqual(first_frame.exception.code, "motion_context_first_frame_conflict")

        portrait = self.request()
        portrait["parameters"]["aspect_ratio"] = "9:16"
        created = self.manager.create({
            "title": "dimensions",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {"continuation": "motion_context", "request": portrait},
            ],
        })
        self.manager.run(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] in {"completed", "failed"})
        failed = self.manager.get(created["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("matching adjacent resolutions", failed["segments"][1]["error"])

    def test_terminal_coordinator_failure_cleans_staged_motion_context(self) -> None:
        job_id = "e" * 32
        staged = self.manager.motion_contexts.input_root / f"{job_id}-{'f' * 32}.latent"
        staged.write_bytes(b"staged")
        self.jobs.put(job_id, {
            "id": job_id,
            "status": "failed",
            "motion_context_staged_file": staged.name,
        })
        value = self.manager._poll_job(job_id)
        self.assertEqual(value["status"], "failed")
        self.assertNotIn("motion_context_staged_file", value)
        self.assertFalse(staged.exists())
        self.assertNotIn("motion_context_staged_file", self.jobs.get(job_id))

    def test_practical_limit_allows_65_but_rejects_1001_without_duration_cap(self) -> None:
        body = {"title": "Many clips", "segments": [{"continuation": "none", "request": self.request()}] * 65}
        created = self.manager.create(body)
        self.assertEqual(len(created["segments"]), 65)
        with self.assertRaises(ApiError) as raised:
            self.manager.create({**body, "segments": body["segments"] + [body["segments"][0]] * 936})
        self.assertEqual(raised.exception.code, "invalid_segments")

    def test_segment_duration_accepts_362_frames_and_rejects_more(self) -> None:
        request = self.request()
        request["parameters"]["duration"] = H3_MAX_DURATION_SECONDS
        created = self.create_project(["none"], [request])
        self.assertEqual(
            created["segments"][0]["request"]["parameters"]["duration"],
            H3_MAX_DURATION_SECONDS,
        )
        request["parameters"]["duration"] = H3_MAX_DURATION_SECONDS + 0.001
        with self.assertRaisesRegex(ApiError, "between 5 and 15.0833"):
            self.create_project(["none"], [request])

    def test_storyboard_and_source_range_are_strict_durable_public_contracts(self) -> None:
        source_id = "a" * 32
        source_media = {**MEDIA, "duration": 20.0, "video_duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", source_media)
        storyboard = {
            "source_asset_id": source_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [120, 240, 360],
        }
        source_range = {
            "asset_id": source_id, "start_frame": 120,
            "end_frame": 360, "fps": 24.0,
        }
        created = self.manager.create({
            "title": "Storyboard contract",
            "storyboard": storyboard,
            "segments": [{
                "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                "source_range": source_range,
            }],
        })
        self.assertEqual(created["storyboard"], storyboard)
        self.assertEqual(created["segments"][0]["source_range"], source_range)
        stored = self.manager.store.get(created["id"])
        self.assertEqual(stored["storyboard"], storyboard)
        self.assertEqual(stored["segments"][0]["source_range"], source_range)
        encoded = json.dumps(stored)
        self.assertNotIn("stored_name", encoded)
        self.assertNotIn("comfy_path", encoded)
        self.assertNotIn(str(self.settings.comfy_input), encoded)

    def test_storyboard_rejects_bad_assets_numbers_and_cut_sequences(self) -> None:
        video_id = "b" * 32
        image_id = "c" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(video_id, "video", media)
        self.add_asset(image_id, "image")
        base = {
            "source_asset_id": video_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [120, 240],
        }
        cases = (
            ({**base, "extra": True}, "invalid_storyboard"),
            ({**base, "source_asset_id": image_id}, "source_asset_not_video"),
            ({**base, "source_asset_id": "d" * 32}, "not_found"),
            ({**base, "fps": float("nan")}, "invalid_storyboard"),
            ({**base, "fps": float("inf")}, "invalid_storyboard"),
            ({**base, "fps": 241.0}, "invalid_storyboard"),
            ({**base, "frame_count": 10_000_001}, "invalid_storyboard"),
            ({**base, "fps": 25.0}, "storyboard_source_mismatch"),
            ({**base, "frame_count": 479}, "storyboard_source_mismatch"),
            ({**base, "cut_frames": [120, 120]}, "invalid_storyboard"),
            ({**base, "cut_frames": [240, 120]}, "invalid_storyboard"),
            ({**base, "cut_frames": [0]}, "invalid_storyboard"),
            ({**base, "cut_frames": [480]}, "invalid_storyboard"),
            ({**base, "cut_frames": list(range(1, 202))}, "invalid_storyboard"),
        )
        for storyboard, code in cases:
            with self.subTest(code=code, storyboard=storyboard), self.assertRaises(ApiError) as raised:
                self.manager.create({
                    "title": "Bad storyboard", "storyboard": storyboard,
                    "segments": [],
                })
            self.assertEqual(raised.exception.code, code)

    def test_source_range_enforces_source_bounds_fps_and_h3_frame_contract(self) -> None:
        source_id = "e" * 32
        other_id = "f" * 32
        image_id = "1" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", media)
        self.add_asset(other_id, "video", media)
        self.add_asset(image_id, "image")
        storyboard = {
            "source_asset_id": source_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [],
        }

        def body(source_range):
            return {
                "title": "Range contract", "storyboard": storyboard,
                "segments": [{
                    "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                    "source_range": source_range,
                }],
            }

        accepted = self.manager.create(body({
            "asset_id": source_id, "start_frame": 0,
            "end_frame": 362, "fps": 24.0,
        }))
        self.assertEqual(accepted["segments"][0]["source_range"]["end_frame"], 362)
        cases = (
            ({"asset_id": source_id, "start_frame": 0, "end_frame": 363, "fps": 24.0}, "source_range_duration"),
            ({"asset_id": source_id, "start_frame": -1, "end_frame": 20, "fps": 24.0}, "source_range_bounds"),
            ({"asset_id": source_id, "start_frame": 20, "end_frame": 20, "fps": 24.0}, "source_range_bounds"),
            ({"asset_id": source_id, "start_frame": 0, "end_frame": 481, "fps": 24.0}, "source_range_bounds"),
            ({"asset_id": source_id, "start_frame": 0, "end_frame": 120, "fps": 30.0}, "source_range_fps_mismatch"),
            ({"asset_id": other_id, "start_frame": 0, "end_frame": 120, "fps": 24.0}, "source_range_asset_mismatch"),
            ({"asset_id": image_id, "start_frame": 0, "end_frame": 120, "fps": 24.0}, "source_asset_not_video"),
            ({"asset_id": source_id, "start_frame": 0, "end_frame": 120, "fps": 24.0, "path": "/tmp/x"}, "invalid_source_range"),
            ({"asset_id": source_id, "start_frame": False, "end_frame": 120, "fps": 24.0}, "invalid_source_range"),
        )
        for source_range, code in cases:
            with self.subTest(code=code, source_range=source_range), self.assertRaises(ApiError) as raised:
                self.manager.create(body(source_range))
            self.assertEqual(raised.exception.code, code)

    def test_source_range_requires_ref2va_and_reserves_a_reference_slot(self) -> None:
        source_id = "3" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", media)
        source_range = {
            "asset_id": source_id, "start_frame": 0,
            "end_frame": 120, "fps": 24.0,
        }
        storyboard = {
            "source_asset_id": source_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [],
        }
        with self.assertRaises(ApiError) as raised:
            self.manager.create({
                "title": "FL cannot consume source video",
                "storyboard": storyboard,
                "segments": [{
                    "continuation": "none", "request": self.request(),
                    "source_range": source_range,
                }],
            })
        self.assertEqual(raised.exception.code, "source_range_profile")

        reference_ids = [f"{number + 32:032x}" for number in range(12)]
        reference_kinds = ["image"] * 9 + ["audio"] * 2 + ["video"]
        for asset_id, kind in zip(reference_ids, reference_kinds, strict=True):
            media = {"duration": 2.0, "has_audio": kind == "audio", **({"fps": 24, "reference_fps": 24} if kind == "video" else {})}
            self.add_asset(asset_id, kind, media)
        request = self.request("minimax-h3-ref2va")
        request["references"] = [
            {"asset_id": asset_id, "role": "reference"}
            for asset_id in reference_ids
        ]
        with self.assertRaises(ApiError) as raised:
            self.manager.create({
                "title": "Thirteen files including source", "storyboard": storyboard,
                "segments": [{
                    "continuation": "none", "request": request,
                    "source_range": source_range,
                }],
            })
        self.assertEqual(raised.exception.code, "too_many_references")

        request["references"] = request["references"][:11]
        accepted = self.manager.create({
            "title": "Exactly twelve files including source", "storyboard": storyboard,
            "segments": [{
                "continuation": "none", "request": request,
                "source_range": source_range,
            }],
        })
        self.assertEqual(len(accepted["segments"][0]["request"]["references"]), 11)

        request["references"] = [
            {"asset_id": asset_id, "role": "reference"}
            for asset_id in reference_ids[:11]
        ]
        with self.assertRaises(ApiError) as raised:
            self.manager.create({
                "title": "Source plus continuation reserves two", "storyboard": storyboard,
                "segments": [
                    {"continuation": "none", "request": self.request()},
                    {
                        "continuation": "previous_video", "request": request,
                        "source_range": source_range,
                    },
                ],
            })
        self.assertEqual(raised.exception.code, "too_many_references")

    def test_source_range_is_exactly_trimmed_and_compiled_as_h3_video_reference(self) -> None:
        source_id = "4" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", media)
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomsource-range")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        created = self.manager.create({
            "title": "Exact source frames",
            "storyboard": {
                "source_asset_id": source_id, "fps": 24.0,
                "frame_count": 480, "cut_frames": [37, 157],
            },
            "segments": [{
                "continuation": "none",
                "request": self.request("minimax-h3-ref2va"),
                "source_range": {
                    "asset_id": source_id, "start_frame": 37,
                    "end_frame": 157, "fps": 24.0,
                },
            }],
        })
        prepared, evidence = self.manager._prepare_request(
            self.manager.store.get(created["id"]), 0,
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][commands[0].index("-vf") + 1],
            "trim=start_frame=37:end_frame=157,setpts=PTS-STARTPTS",
        )
        self.assertEqual(commands[0][commands[0].index("-frames:v") + 1], "120")
        source_evidence = evidence["source_range"]
        self.assertEqual(source_evidence["frame_count"], 120)
        self.assertEqual(source_evidence["duration"], 5.0)
        derived = self.assets.get(source_evidence["asset_id"])
        self.assertEqual(derived["derived"]["kind"], "source_range")
        self.assertEqual(derived["derived"]["start_frame"], 37)
        self.assertNotIn(derived["id"], {asset["id"] for asset in self.assets.list_public()})

        spec = parse_generation_request(
            {**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY,
        )
        self.assertEqual(spec.compiler, "h3_ref")
        self.assertEqual([(ref.kind, ref.role) for ref in spec.references], [("video", "motion")])
        self.assertIn("<Video 1>", spec.prompt)
        workflow = compile_video_workflow(spec, self.settings, "8" * 32)
        self.assertEqual(workflow["8"]["class_type"], "MiniMaxH3ReferenceToVideo")
        load_video = next(
            node for node in workflow.values() if node.get("class_type") == "LoadVideo"
        )
        self.assertEqual(load_video["inputs"]["file"], derived["comfy_path"])

        stored = self.manager.store.get(created["id"])
        stored["segments"][0]["attempts"] = [{
            "id": "9" * 32, "status": "failed", "continuation": evidence,
        }]
        self.manager.store.put(created["id"], stored)
        with patch.object(self.manager, "_start"):
            self.manager.rerun_segment(created["id"], stored["segments"][0]["id"])
        with self.assertRaises(ApiError):
            self.assets.get(derived["id"])
        reclaimed = self.manager.get(created["id"])["segments"][0]["attempts"][0]["continuation"]["source_range"]
        self.assertIn("asset_reclaimed_at", reclaimed)

    def test_source_range_multi_video_inputs_remain_honest_generic_r2v(self) -> None:
        source_id = "5" * 32
        explicit_video_id = "6" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", media)
        self.add_asset(explicit_video_id, "video", dict(MEDIA))
        source_range = {
            "asset_id": source_id, "start_frame": 0,
            "end_frame": 120, "fps": 24.0,
        }
        request = self.request("minimax-h3-ref2va")
        request["references"] = [
            {"asset_id": explicit_video_id, "role": "camera", "include_audio": False},
        ]
        request["prompt"] += f"; @{{{explicit_video_id}}}"
        created = self.manager.create({
            "title": "source range plus explicit video",
            "storyboard": {
                "source_asset_id": source_id, "fps": 24.0,
                "frame_count": 480, "cut_frames": [],
            },
            "segments": [{
                "continuation": "none", "request": request,
                "source_range": source_range,
            }],
        })
        self.assertEqual(created["segments"][0]["status"], "pending")

        derived_source_id = "7" * 32
        continuation_id = "8" * 32
        self.add_asset(derived_source_id, "video", dict(MEDIA))
        self.add_asset(continuation_id, "video", dict(MEDIA))
        prepared = self.manager._with_source_range_reference(
            self.request("minimax-h3-ref2va"), derived_source_id,
        )
        self.assertEqual(prepared["director_mode"], "v2v")
        prepared = self.manager._with_continuation_reference(
            prepared, "previous_video", continuation_id,
        )
        self.assertEqual(prepared["director_mode"], "r2v")
        self.assertNotIn("source_asset_id", prepared)
        spec = parse_generation_request(
            {**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY,
        )
        self.assertEqual(spec.director_mode, "r2v")
        self.assertEqual(spec.source_asset_id, "")
        self.assertEqual([ref.kind for ref in spec.references], ["video", "video"])

    def test_source_range_preparation_rejects_hash_change_and_path_escape_before_ffmpeg(self) -> None:
        for case in ("hash", "symlink"):
            with self.subTest(case=case):
                asset_id = ("a" if case == "hash" else "b") * 32
                self.add_asset(asset_id, "video", {**MEDIA, "duration": 10.0, "frame_count": 240})
                asset = self.assets.get(asset_id)
                if case == "hash":
                    asset["sha256"] = "0" * 64
                    expected = "source_integrity"
                else:
                    path = self.settings.comfy_input / "h3-studio" / asset["stored_name"]
                    path.unlink()
                    outside = self.root / "outside-source.mp4"
                    outside.write_bytes(b"outside")
                    path.symlink_to(outside)
                    expected = "unsafe_path"
                self.assets.metadata.put(asset_id, asset)
                created = self.manager.create({
                    "title": f"Reject {case}",
                    "storyboard": {
                        "source_asset_id": asset_id, "fps": 24.0,
                        "frame_count": 240, "cut_frames": [],
                    },
                    "segments": [{
                        "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                        "source_range": {
                            "asset_id": asset_id, "start_frame": 0,
                            "end_frame": 120, "fps": 24.0,
                        },
                    }],
                })
                commands = []
                self.manager.command_runner = lambda *args, **kwargs: commands.append((args, kwargs))
                with self.assertRaises(ApiError) as raised:
                    self.manager._prepare_request(self.manager.store.get(created["id"]), 0)
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(commands, [])

    def test_failed_source_range_submit_reclaims_private_crop_immediately(self) -> None:
        source_id = "c" * 32
        self.add_asset(source_id, "video", {**MEDIA, "duration": 10.0, "frame_count": 240})

        def ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomfailed-submit")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        created = self.manager.create({
            "title": "Failed submit cleanup",
            "storyboard": {
                "source_asset_id": source_id, "fps": 24.0,
                "frame_count": 240, "cut_frames": [],
            },
            "segments": [{
                "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                "source_range": {
                    "asset_id": source_id, "start_frame": 0,
                    "end_frame": 120, "fps": 24.0,
                },
            }],
        })
        with patch.object(self.manager, "_submit_segment", side_effect=ApiError(503, "submit_failed", "injected")):
            self.manager.run(created["id"])
            wait_until(lambda: self.manager.get(created["id"])["status"] == "failed")
            wait_until(lambda: (
                {asset["id"] for asset in self.assets.list()} == {source_id}
                and "asset_reclaimed_at" in self.manager.get(created["id"])["segments"][0]["attempts"][0]["continuation"]["source_range"]
            ))
        self.assertEqual({asset["id"] for asset in self.assets.list()}, {source_id})
        evidence = self.manager.get(created["id"])["segments"][0]["attempts"][0]["continuation"]["source_range"]
        self.assertIn("asset_reclaimed_at", evidence)

    def test_character_migration_tail_padding_is_private_exact_and_audio_aligned(self) -> None:
        source_id, character_id = "4" * 32, "5" * 32
        self.add_asset(source_id, "video", {
            **MEDIA, "duration": 3.0, "video_duration": 3.0,
            "frame_count": 72, "reference_fps": 24.0,
        })
        self.add_asset(character_id, "image", {"width": 64, "height": 64})
        source = self.assets.get(source_id)
        character = self.assets.get(character_id)
        source["sha256"] = hashlib.sha256(self.assets.content_path(source).read_bytes()).hexdigest()
        character["sha256"] = hashlib.sha256(self.assets.content_path(character).read_bytes()).hexdigest()
        self.assets.metadata.put(source_id, source)
        self.assets.metadata.put(character_id, character)
        planned = plan_migration(
            {
                "version": MIGRATION_VERSION, "source_asset_id": source_id,
                "targets": [{"character_asset_id": character_id, "source_subject": "the centered dancer"}],
                "segment_frames": 124, "overlap_frames": 5,
                "audio_policy": "reference-source",
            },
            source=source, character=character, registry=DEFAULT_REGISTRY,
        )
        created = self.manager.create(planned["project"])
        commands = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisompadded")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        self.probe.stop()
        try:
            with patch.object(AssetStore, "_probe_media", return_value=dict(MEDIA)):
                prepared, evidence = self.manager._prepare_request(
                    self.manager.store.get(created["id"]), 0,
                )
        finally:
            self.probe.start()
        command = commands[0]
        self.assertTrue(any("tpad=stop_mode=clone:stop=52" in part for part in command))
        self.assertTrue(any("apad=pad_dur=5.166666667" in part for part in command))
        self.assertEqual(evidence["source_range"]["frame_count"], 72)
        self.assertEqual(evidence["source_range"]["materialized_frame_count"], 124)
        self.assertEqual(evidence["source_range"]["input_padding_frames"], 52)
        self.assertTrue(prepared["references"][-1]["include_audio"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_real_source_range_crop_preserves_exact_decoded_frame_interval(self) -> None:
        source_id = "d" * 32
        source_path = self.settings.comfy_input / "h3-studio" / f"{source_id}.mp4"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=s=64x64:r=30:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(source_path),
        ], check=True, capture_output=True)
        self.assets.metadata.put(source_id, {
            "id": source_id, "kind": "video", "filename": source_path.name,
            "stored_name": source_path.name, "comfy_path": f"h3-studio/{source_path.name}",
            "media": {"duration": 4.0, "fps": 30.0, "frame_count": 120},
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(), "created_at": time.time(),
        })
        created = self.manager.create({
            "title": "Real exact crop",
            "storyboard": {
                "source_asset_id": source_id, "fps": 30.0,
                "frame_count": 120, "cut_frames": [],
            },
            "segments": [{
                "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                "source_range": {
                    "asset_id": source_id, "start_frame": 15,
                    "end_frame": 75, "fps": 30.0,
                },
            }],
        })
        self.assets.config = replace(self.settings, max_video_bytes=10 * 1024 * 1024)
        _, evidence = self.manager._prepare_request(self.manager.store.get(created["id"]), 0)
        crop = self.assets.content_path(self.assets.get(evidence["source_range"]["asset_id"]))
        probe = json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames,avg_frame_rate,duration", "-of", "json", str(crop),
        ], check=True, capture_output=True, text=True).stdout)["streams"][0]
        self.assertEqual(int(probe["nb_frames"]), 60)
        self.assertEqual(probe["avg_frame_rate"], "30/1")
        self.assertAlmostEqual(float(probe["duration"]), 2.0, places=3)

    def test_source_range_change_stales_segment_and_only_continuation_downstream(self) -> None:
        source_id = "2" * 32
        media = {**MEDIA, "duration": 20.0, "frame_count": 480}
        self.add_asset(source_id, "video", media)
        storyboard = {
            "source_asset_id": source_id, "fps": 24.0,
            "frame_count": 480, "cut_frames": [120, 240],
        }
        ranges = [
            {"asset_id": source_id, "start_frame": 0, "end_frame": 120, "fps": 24.0},
            {"asset_id": source_id, "start_frame": 120, "end_frame": 240, "fps": 24.0},
            {"asset_id": source_id, "start_frame": 240, "end_frame": 360, "fps": 24.0},
        ]
        created = self.manager.create({
            "title": "Stale ranges", "storyboard": storyboard,
            "segments": [
                {
                    "continuation": mode,
                    "request": self.request("minimax-h3-ref2va") if index != 1 else self.request(),
                    **({"source_range": ranges[index]} if index != 1 else {}),
                }
                for index, mode in enumerate(("none", "tail_frame", "none"))
            ],
        })
        stored = self.manager.store.get(created["id"])
        for index, segment in enumerate(stored["segments"]):
            segment.update({"status": "completed", "job_id": f"{index + 10:032x}"})
        stored["status"] = "completed"
        self.manager.store.put(created["id"], stored)
        changed = dict(ranges[0])
        changed.update({"start_frame": 24, "end_frame": 144})
        updated = self.manager.update(created["id"], {
            "title": "Stale ranges", "storyboard": storyboard,
            "segments": [
                {
                    "id": created["segments"][index]["id"],
                    "continuation": mode,
                    "request": self.request("minimax-h3-ref2va") if index != 1 else self.request(),
                    **(
                        {"source_range": changed if index == 0 else ranges[index]}
                        if index != 1 else {}
                    ),
                }
                for index, mode in enumerate(("none", "tail_frame", "none"))
            ],
        })
        self.assertEqual([segment["status"] for segment in updated["segments"]], ["stale", "stale", "completed"])
        self.assertEqual(updated["segments"][0]["error"], "source range changed")
        self.assertEqual(updated["segments"][1]["error"], "upstream source range changed")
        self.assertNotIn("job_id", updated["segments"][0])
        self.assertNotIn("job_id", updated["segments"][1])
        self.assertIn("job_id", updated["segments"][2])

    def test_legacy_projects_without_storyboard_or_source_range_remain_compatible(self) -> None:
        created = self.create_project(["none"])
        receipt = self.manager.get(created["id"])
        self.assertNotIn("storyboard", receipt)
        self.assertNotIn("source_range", receipt["segments"][0])
        updated = self.manager.update(created["id"], {
            "title": "Legacy updated",
            "segments": [{
                "id": created["segments"][0]["id"],
                "continuation": "none", "request": self.request(),
            }],
        })
        self.assertNotIn("storyboard", updated)
        self.assertNotIn("source_range", updated["segments"][0])

    def test_both_continuation_modes_reserve_one_reference_slot(self) -> None:
        ids = [f"{number:032x}" for number in range(1, 13)]
        for asset_id in ids:
            self.add_asset(asset_id, "image")
        for continuation, profile_id, role in (
            ("tail_frame", "minimax-h3-fl2va", "last_frame"),
            ("previous_video", "minimax-h3-ref2va", "identity"),
        ):
            request = self.request(profile_id)
            selected = ids[:2] if continuation == "tail_frame" else ids
            request["references"] = [{"asset_id": asset_id, "role": role} for asset_id in selected]
            with self.subTest(continuation=continuation), self.assertRaises(ApiError) as raised:
                self.create_project(["none", continuation], [self.request(), request])
            self.assertEqual(raised.exception.code, "too_many_references")

    def test_background_execution_is_strictly_sequential_and_durable(self) -> None:
        created = self.create_project(["none", "none"])
        self.manager.run(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "completed")
        receipt = self.manager.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 2)
        self.assertEqual([segment["status"] for segment in receipt["segments"]], ["completed", "completed"])
        self.assertTrue(all(len(segment["attempts"]) == 1 for segment in receipt["segments"]))
        reloaded = VideoProjectManager(
            self.settings, self.assets, self.jobs, self.comfy, DEFAULT_REGISTRY, threading.RLock(),
        ).get(created["id"])
        self.assertEqual(reloaded["segments"][1]["attempts"][0]["status"], "completed")

    def test_segment_attempt_proves_nondefault_generation_strength(self) -> None:
        request = self.request()
        request["parameters"]["denoise"] = 0.65
        created = self.create_project(["none"], [request])
        self.manager.run(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "completed")
        receipt = self.manager.get(created["id"])
        attempt = receipt["segments"][0]["attempts"][0]
        job = self.jobs.get(attempt["job_id"])
        self.assertEqual(self.comfy.workflows[0]["12"]["inputs"]["denoise"], 0.65)
        self.assertEqual(job["parameters"]["denoise"], 0.65)
        self.assertEqual(job["workflow_evidence"]["denoise"], 0.65)
        self.assertEqual(attempt["workflow_evidence"]["denoise"], 0.65)

    def test_run_is_idempotent_and_stop_cancels_active_comfy_prompt(self) -> None:
        self.comfy.complete = False
        created = self.create_project(["none"])
        self.manager.run(created["id"])
        wait_until(lambda: self.comfy.submit_count == 1)
        self.manager.run(created["id"])
        self.assertEqual(self.comfy.submit_count, 1)
        self.manager.stop(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "stopped")
        self.assertEqual(self.comfy.canceled, ["prompt-1"])

    def test_stop_accepted_during_preparation_prevents_any_paid_submit(self) -> None:
        created = self.create_project(["none"])
        entered = threading.Event()
        release = threading.Event()
        original = self.manager._prepare_request

        def blocked(project, index):
            entered.set()
            release.wait(2)
            return original(project, index)

        with patch.object(self.manager, "_prepare_request", side_effect=blocked):
            self.manager.run(created["id"])
            self.assertTrue(entered.wait(1))
            stopped = self.manager.stop(created["id"])
            self.assertEqual(stopped["status"], "stopping")
            release.set()
            wait_until(lambda: self.manager.get(created["id"])["status"] == "stopped")
        self.assertEqual(self.comfy.submit_count, 0)

    def test_stop_during_capability_compile_window_terminalizes_as_canceled(self) -> None:
        created = self.create_project(["none"])
        entered = threading.Event()
        release = threading.Event()

        def blocked(*_args):
            entered.set()
            release.wait(2)

        self.comfy.ensure_capability = blocked
        self.manager.run(created["id"])
        self.assertTrue(entered.wait(1))
        stopped = self.manager.stop(created["id"])
        self.assertEqual(stopped["status"], "stopping")
        release.set()
        wait_until(lambda: self.manager.get(created["id"])["status"] == "stopped")
        receipt = self.manager.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 0)
        self.assertEqual(receipt["segments"][0]["status"], "stopped")
        self.assertEqual(receipt["segments"][0]["attempts"][0]["status"], "canceled")
        self.assertEqual(self.jobs.get(receipt["segments"][0]["job_id"])["status"], "canceled")

    def test_live_stop_retries_failed_remote_cancel_without_marking_failed(self) -> None:
        self.comfy.complete = False
        created = self.create_project(["none"])
        self.manager.run(created["id"])
        wait_until(lambda: self.comfy.submit_count == 1)
        original_cancel = self.comfy.cancel
        calls = 0

        def flaky(prompt_id):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ApiError(502, "cancel_failed", "remote unavailable")
            original_cancel(prompt_id)

        self.comfy.cancel = flaky
        stopped = self.manager.stop(created["id"])
        self.assertEqual(stopped["status"], "stopping")
        self.assertEqual(self.jobs.list()[0]["status"], "queued")
        wait_until(lambda: self.manager.get(created["id"])["status"] == "stopped", timeout=6)
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(self.jobs.list()[0]["status"], "canceled")

    def test_restart_cancellation_failure_remains_stopping_and_job_active(self) -> None:
        self.comfy.complete = False
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "e" * 32
        attempt_id = "f" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "queued", "prompt_id": "remote-prompt", "client_id": "client"})
        project.update({"status": "stopping", "current_index": 0, "stop_requested": True})
        project["segments"][0].update({"status": "running", "job_id": job_id, "attempts": [{"id": attempt_id, "status": "queued", "job_id": job_id}]})
        self.manager.store.put(created["id"], project)
        self.comfy.cancel = lambda _prompt_id: (_ for _ in ()).throw(ApiError(502, "cancel_failed", "remote unavailable"))
        self.assertFalse(self.manager._reconcile_stopping(project))
        receipt = self.manager.get(created["id"])
        self.assertEqual(receipt["status"], "stopping")
        self.assertEqual(receipt["segments"][0]["attempts"][0]["status"], "queued")
        self.assertEqual(self.jobs.get(job_id)["status"], "queued")

    def test_rerun_marks_only_transitively_dependent_chain_stale(self) -> None:
        ref = self.request("minimax-h3-ref2va")
        created = self.create_project(
            ["none", "tail_frame", "previous_video", "none", "tail_frame"],
            [self.request(), self.request(), ref, self.request(), self.request()],
        )
        project = self.manager.store.get(created["id"])
        for index, segment in enumerate(project["segments"]):
            segment.update({"status": "completed", "job_id": f"{index + 100:032x}"})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        with patch.object(self.manager, "_start"):
            receipt = self.manager.rerun_segment(created["id"], project["segments"][0]["id"])
        self.assertEqual([s["status"] for s in receipt["segments"]], ["pending", "stale", "stale", "completed", "completed"])

    def test_put_change_marks_only_transitively_dependent_chain_stale(self) -> None:
        ref = self.request("minimax-h3-ref2va")
        created = self.create_project(
            ["none", "tail_frame", "previous_video", "none", "tail_frame"],
            [self.request(), self.request(), ref, self.request(), self.request()],
        )
        project = self.manager.store.get(created["id"])
        for index, segment in enumerate(project["segments"]):
            segment.update({"status": "completed", "job_id": f"{index + 200:032x}"})
        project["status"] = "completed"
        project["merged"] = {"status": "completed"}
        self.manager.store.put(created["id"], project)
        definitions = []
        for segment in project["segments"]:
            request = dict(segment["request"])
            if segment["index"] == 0:
                request["prompt"] += " with a red scarf"
            definitions.append({"id": segment["id"], "continuation": segment["continuation"], "request": request})
        receipt = self.manager.update(created["id"], {"title": "Revised", "segments": definitions})
        self.assertEqual([s["status"] for s in receipt["segments"]], ["pending", "stale", "stale", "completed", "completed"])
        self.assertNotIn("merged", receipt)

    def test_tail_frame_is_imported_and_adds_only_its_typed_prompt_tag(self) -> None:
        created = self.create_project(["none", "tail_frame"])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source video")
        job_id = "a" * 32
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": digest, "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})

        observed_commands = []

        def ffmpeg(command, **_kwargs):
            observed_commands.append(command)
            Path(command[-1]).write_bytes(b"\x89PNG\r\n\x1a\nframe")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        request, evidence = self.manager._prepare_request(project, 1)
        self.assertEqual(evidence["source_job_id"], job_id)
        self.assertEqual(request["references"][0]["role"], "first_frame")
        self.assertIn("-update", observed_commands[0])
        self.assertNotIn("-fps_mode", observed_commands[0])
        spec = parse_generation_request({**request, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(spec.prompt, "A cream robot continues walking through a living room; <Picture 1>")
        self.assertNotIn("at 0.00 seconds", spec.prompt)

    def test_previous_video_is_staged_as_silent_motion_reference(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "b" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomsilent")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        request, evidence = self.manager._prepare_request(project, 1)
        reference = request["references"][-1]
        self.assertEqual((reference["role"], reference["include_audio"]), ("motion", False))
        self.assertEqual(self.assets.get(evidence["asset_id"])["media"]["fps"], 24)
        self.assertFalse(evidence["trimmed_for_reference"])
        self.assertIn("-an", commands[0])
        self.assertEqual(commands[0][commands[0].index("-c:v") + 1], "copy")
        self.assertEqual(evidence["reference_transform"], "video_only_stream_copy")
        self.assertTrue(evidence["source_had_audio"])
        self.assertFalse(evidence["reference_has_audio"])

    def test_continuation_range_is_strict_normalized_public_and_keeps_one_slot(self) -> None:
        explicit_ids = [f"{value:032x}" for value in range(1, 6)]
        for asset_id in explicit_ids:
            self.add_asset(asset_id, "image", {"width": 1024, "height": 1024})
        followup = self.request("minimax-h3-ref2va")
        followup["references"] = [
            {"asset_id": asset_id, "role": "reference"}
            for asset_id in explicit_ids
        ]
        predecessor = self.request()
        predecessor["parameters"]["duration"] = H3_MAX_DURATION_SECONDS
        continuation_range = {"start_frame": 24, "end_frame": 360, "fps": 24}
        created = self.manager.create({
            "title": "Selected previous interval",
            "segments": [
                {"continuation": "none", "request": predecessor},
                {
                    "continuation": "previous_video", "request": followup,
                    "continuation_range": continuation_range,
                },
            ],
        })
        normalized = {"start_frame": 24, "end_frame": 360, "fps": 24.0}
        self.assertEqual(created["segments"][1]["continuation_range"], normalized)
        stored = self.manager.store.get(created["id"])
        self.assertEqual(stored["segments"][1]["continuation_range"], normalized)
        self.assertEqual(self.manager.get(created["id"])["segments"][1]["continuation_range"], normalized)

        cases = (
            ({"start_frame": 0, "end_frame": 120, "fps": 24}, "tail_frame", "continuation_range_mode"),
            ({"start_frame": 0, "end_frame": 120, "fps": 30}, "previous_video", "continuation_range_fps"),
            ({"start_frame": -1, "end_frame": 120, "fps": 24}, "previous_video", "continuation_range_bounds"),
            ({"start_frame": 120, "end_frame": 120, "fps": 24}, "previous_video", "continuation_range_bounds"),
            ({"start_frame": 0, "end_frame": 361, "fps": 24}, "previous_video", "continuation_range_duration"),
            ({"start_frame": 10000, "end_frame": 10300, "fps": 24}, "previous_video", "continuation_range_bounds"),
            ({"start_frame": 0, "end_frame": 120, "fps": 24, "path": "/tmp/x"}, "previous_video", "invalid_continuation_range"),
        )
        for value, mode, code in cases:
            request = self.request() if mode == "tail_frame" else self.request("minimax-h3-ref2va")
            with self.subTest(code=code), self.assertRaises(ApiError) as raised:
                self.manager.create({
                    "title": "Invalid interval",
                    "segments": [
                        {"continuation": "none", "request": self.request()},
                        {
                            "continuation": mode, "request": request,
                            "continuation_range": value,
                        },
                    ],
                })
            self.assertEqual(raised.exception.code, code)

    def test_continuation_range_change_stales_current_and_continuation_chain(self) -> None:
        initial = {"start_frame": 0, "end_frame": 120, "fps": 24}
        predecessor = self.request()
        predecessor["parameters"]["duration"] = 10
        created = self.manager.create({
            "title": "Continuation invalidation",
            "segments": [
                {"continuation": "none", "request": predecessor},
                {
                    "continuation": "previous_video",
                    "request": self.request("minimax-h3-ref2va"),
                    "continuation_range": initial,
                },
                {"continuation": "tail_frame", "request": self.request()},
                {"continuation": "none", "request": self.request()},
            ],
        })
        stored = self.manager.store.get(created["id"])
        for index, segment in enumerate(stored["segments"]):
            segment.update({"status": "completed", "job_id": f"{index + 40:032x}"})
        stored["status"] = "completed"
        stored["merged"] = {"status": "completed"}
        self.manager.store.put(created["id"], stored)

        changed = {"start_frame": 24, "end_frame": 144, "fps": 24}
        updated = self.manager.update(created["id"], {
            "title": "Continuation invalidation",
            "segments": [
                {
                    "id": segment["id"], "continuation": segment["continuation"],
                    "request": segment["request"],
                    **(
                        {"continuation_range": changed}
                        if index == 1 else {}
                    ),
                }
                for index, segment in enumerate(created["segments"])
            ],
        })
        self.assertEqual(
            [segment["status"] for segment in updated["segments"]],
            ["completed", "stale", "stale", "completed"],
        )
        self.assertEqual(updated["segments"][1]["error"], "continuation range changed")
        self.assertEqual(updated["segments"][2]["error"], "upstream continuation range changed")
        self.assertNotIn("job_id", updated["segments"][1])
        self.assertNotIn("job_id", updated["segments"][2])
        self.assertNotIn("merged", self.manager.store.get(created["id"]))

    def test_continuation_range_extracts_exact_silent_frames_and_records_evidence(self) -> None:
        continuation_range = {"start_frame": 24, "end_frame": 96, "fps": 24}
        created = self.manager.create({
            "title": "Exact previous interval",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {
                    "continuation": "previous_video",
                    "request": self.request("minimax-h3-ref2va"),
                    "continuation_range": continuation_range,
                },
            ],
        })
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "range-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "a" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomselected")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        selected_media = {
            **SILENT_DERIVED_MEDIA,
            "duration": 3.0, "video_duration": 3.0,
            "fps": 24.0, "frame_count": 72,
        }
        with patch.object(AssetStore, "_probe_media", return_value=selected_media):
            request, evidence = self.manager._prepare_request(project, 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0][commands[0].index("-vf") + 1],
            "trim=start_frame=24:end_frame=96,setpts=PTS-STARTPTS",
        )
        self.assertEqual(commands[0][commands[0].index("-frames:v") + 1], "72")
        self.assertIn("-an", commands[0])
        interval = evidence["continuation_range"]
        self.assertEqual(interval["requested"]["start_time"], 1.0)
        self.assertEqual(interval["requested"]["end_time"], 4.0)
        self.assertEqual(interval["requested"]["frame_count"], 72)
        self.assertEqual(interval["effective"]["frame_count"], 72)
        self.assertEqual(interval["effective"]["fps"], 24.0)
        self.assertEqual(interval["effective"]["duration"], 3.0)
        self.assertEqual(interval["effective"]["end_time"], 4.0)
        self.assertEqual(interval["source"]["frame_count"], MEDIA["frame_count"])
        self.assertEqual(evidence["reference_transform"], "video_only_exact_frame_range_reencode")
        self.assertFalse(evidence["reference_has_audio"])
        derived = self.assets.get(evidence["asset_id"])
        self.assertEqual(derived["derived"]["kind"], "continuation")
        self.assertEqual(derived["derived"]["start_frame"], 24)
        self.assertNotIn(derived["id"], {asset["id"] for asset in self.assets.list_public()})
        self.assertEqual(request["references"][-1]["asset_id"], derived["id"])

    def test_continuation_range_rejects_mismatched_derived_media_and_deletes_it(self) -> None:
        created = self.manager.create({
            "title": "Reject mismatched crop",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {
                    "continuation": "previous_video",
                    "request": self.request("minimax-h3-ref2va"),
                    "continuation_range": {"start_frame": 24, "end_frame": 96, "fps": 24},
                },
            ],
        })
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "mismatch-range-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "e" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()

        # The suite-level probe reports 124 frames. That must no longer pass
        # as an allegedly effective 72-frame crop.
        with self.assertRaises(ApiError) as raised:
            self.manager._prepare_request(project, 1)
        self.assertEqual(raised.exception.code, "continuation_range_output_mismatch")
        self.assertFalse(any(
            isinstance(asset.get("derived"), dict)
            and asset["derived"].get("segment_id") == project["segments"][1]["id"]
            for asset in self.assets.list()
        ))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_real_continuation_range_crop_reports_verified_effective_media(self) -> None:
        self.assets.config = replace(self.assets.config, max_video_bytes=2 * 1024 * 1024)
        created = self.manager.create({
            "title": "Real exact previous crop",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {
                    "continuation": "previous_video",
                    "request": self.request("minimax-h3-ref2va"),
                    "continuation_range": {"start_frame": 24, "end_frame": 96, "fps": 24},
                },
            ],
        })
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "real-range-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=32x32:rate=24:duration=5.1666666667",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5.1666666667",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(source),
        ], check=True)
        source_media = REAL_PROBE_MEDIA(source, "video")
        job_id = "f" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": source_media,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})

        with patch.object(AssetStore, "_probe_media", side_effect=REAL_PROBE_MEDIA):
            _, evidence = self.manager._prepare_request(project, 1)
        effective = evidence["continuation_range"]["effective"]
        self.assertEqual(effective["frame_count"], 72)
        self.assertAlmostEqual(effective["fps"], 24.0, places=3)
        self.assertAlmostEqual(effective["duration"], 3.0, places=3)
        self.assertAlmostEqual(effective["start_time"], 1.0, places=3)
        self.assertAlmostEqual(effective["end_time"], 4.0, places=3)
        derived = self.assets.get(evidence["asset_id"])
        self.assertFalse(derived["media"]["has_audio"])
        self.assertEqual(derived["media"]["frame_count"], 72)

    def test_continuation_range_rejects_actual_previous_output_bounds_before_ffmpeg(self) -> None:
        created = self.manager.create({
            "title": "Out of bounds previous interval",
            "segments": [
                {"continuation": "none", "request": self.request()},
                {
                    "continuation": "previous_video",
                    "request": self.request("minimax-h3-ref2va"),
                    "continuation_range": {"start_frame": 0, "end_frame": 120, "fps": 24},
                },
            ],
        })
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "short-range-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "d" * 32
        short_media = {**MEDIA, "duration": 100 / 24, "video_duration": 100 / 24, "frame_count": 100}
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": short_media,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []
        self.manager.command_runner = lambda command, **_kwargs: commands.append(command)

        with self.assertRaises(ApiError) as raised:
            self.manager._prepare_request(project, 1)
        self.assertEqual(raised.exception.code, "continuation_range_out_of_bounds")
        self.assertEqual(commands, [])

    def test_short_previous_video_reencodes_video_only_when_stream_copy_is_unsupported(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "unsupported-copy-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "4" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            if len(commands) == 1:
                return subprocess.CompletedProcess(command, 1, "", "unsupported mux")
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomreencoded")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        _, evidence = self.manager._prepare_request(project, 1)

        self.assertEqual(len(commands), 2)
        self.assertIn("-an", commands[0])
        self.assertIn("-an", commands[1])
        self.assertEqual(commands[0][commands[0].index("-c:v") + 1], "copy")
        self.assertEqual(commands[1][commands[1].index("-c:v") + 1], "libx264")
        self.assertEqual(evidence["reference_transform"], "video_only_reencode")
        self.assertFalse(evidence["reference_has_audio"])

    def test_362_frame_previous_video_is_trimmed_to_the_15_second_reference_contract(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "source-362.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "6" * 32
        source_media = {**MEDIA, "duration": H3_MAX_DURATION_SECONDS, "video_duration": H3_MAX_DURATION_SECONDS, "frame_count": 362}
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": source_media,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomtrimmed")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        trimmed_media = {**MEDIA, "duration": 15.0, "video_duration": 15.0, "frame_count": 360, "has_audio": False}
        with patch.object(AssetStore, "_probe_media", return_value=trimmed_media):
            request, evidence = self.manager._prepare_request(project, 1)

        self.assertEqual(commands[0][commands[0].index("-t") + 1], "15")
        self.assertIn("-an", commands[0])
        self.assertTrue(evidence["trimmed_for_reference"])
        self.assertEqual(evidence["source_duration"], H3_MAX_DURATION_SECONDS)
        self.assertEqual(evidence["reference_duration_limit"], 15.0)
        derived = self.assets.get(evidence["asset_id"])
        self.assertEqual(derived["media"]["duration"], 15.0)
        spec = parse_generation_request({**request, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(spec.references[-1].duration, 15.0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_previous_video_materialization_physically_removes_audio_for_short_and_long_sources(self) -> None:
        self.assets.config = replace(self.assets.config, max_video_bytes=2 * 1024 * 1024)
        for token, duration, expected_transform in (
            ("c", 2.0, "video_only_stream_copy"),
            ("d", 15.2, "video_only_trim_reencode"),
        ):
            with self.subTest(duration=duration):
                created = self.create_project(
                    ["none", "previous_video"],
                    [self.request(), self.request("minimax-h3-ref2va")],
                )
                project = self.manager.store.get(created["id"])
                source = self.settings.comfy_output / f"source-with-audio-{token}.mp4"
                source.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run([
                    "ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=c=blue:s=32x32:r=24:d={duration}",
                    "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source),
                ], check=True)
                job_id = token * 32
                source_media = {
                    **MEDIA, "duration": duration, "video_duration": duration,
                    "frame_count": round(duration * 24), "has_audio": True,
                }
                self.jobs.put(job_id, {
                    "id": job_id, "status": "completed", "outputs": [{
                        "filename": source.name, "subfolder": "", "type": "output",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "media": source_media,
                    }],
                })
                project["segments"][0].update({"status": "completed", "job_id": job_id})

                _, evidence = self.manager._prepare_request(project, 1)
                derived = self.assets.get(evidence["asset_id"])
                derived_path = self.assets.content_path(derived)
                inspected = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                    "-of", "json", str(derived_path),
                ], capture_output=True, text=True, check=True)
                stream_types = [stream["codec_type"] for stream in json.loads(inspected.stdout)["streams"]]

                self.assertEqual(stream_types, ["video"])
                self.assertEqual(evidence["reference_transform"], expected_transform)
                self.assertEqual(evidence["audio_policy"], "video_only")
                self.assertTrue(evidence["source_had_audio"])
                self.assertFalse(evidence["reference_has_audio"])

    def test_previous_video_trims_a_container_with_audio_past_15_seconds(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "source-long-audio.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "7" * 32
        source_media = {**MEDIA, "duration": 15.2, "video_duration": 14.0, "frame_count": 336}
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": source_media,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomtrimmed")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        normalized = {**MEDIA, "duration": 14.0, "video_duration": 14.0, "frame_count": 336, "has_audio": False}
        with patch.object(AssetStore, "_probe_media", return_value=normalized):
            request, evidence = self.manager._prepare_request(project, 1)

        self.assertIn("-an", commands[0])
        self.assertTrue(evidence["trimmed_for_reference"])
        self.assertEqual(evidence["source_duration"], 15.2)
        self.assertEqual(evidence["source_video_duration"], 14.0)
        spec = parse_generation_request({**request, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(spec.references[-1].duration, 14.0)

    def test_previous_video_with_an_explicit_video_stays_generic_r2v(self) -> None:
        explicit_id = "7" * 32
        self.add_asset(explicit_id, "video", dict(MEDIA))
        followup = self.request("minimax-h3-ref2va")
        followup["references"] = [{"asset_id": explicit_id, "role": "camera", "include_audio": False}]
        followup["prompt"] += f"; match the lens movement from @{{{explicit_id}}}"
        created = self.create_project(["none", "previous_video"], [self.request(), followup])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "mixed-reference-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "8" * 32
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": digest, "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()

        prepared, evidence = self.manager._prepare_request(project, 1)
        spec = parse_generation_request({**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(prepared["references"][-1]["asset_id"], evidence["asset_id"])
        self.assertEqual(spec.director_mode, "r2v")
        self.assertEqual(spec.source_asset_id, "")
        self.assertEqual(spec.prompt, "A cream robot continues walking through a living room; match the lens movement from <Video 1>; <Video 2>")
        self.assertNotIn("camera trajectory, scene geography, and screen direction", spec.prompt)

    def test_continuation_preflight_rejects_alias_that_only_matches_synthetic_placeholder(self) -> None:
        followup = self.request("minimax-h3-ref2va")
        followup["prompt"] = "Continue from @{00000000000000000000000000000000}"
        with self.assertRaises(ApiError) as raised:
            self.create_project(["none", "previous_video"], [self.request(), followup])
        self.assertEqual(raised.exception.code, "unknown_reference")

    def test_restart_recovers_existing_attempt_job_without_resubmit(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        attempt_id, job_id = "c" * 32, "d" * 32
        project["status"] = "running"
        project["segments"][0].update({"status": "running", "attempts": [{"id": attempt_id, "status": "preparing", "started_at": time.time(), "continuation": {"mode": "none"}}]})
        self.manager.store.put(created["id"], project)
        output = self.settings.comfy_output / "recovered.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"recovered")
        self.jobs.put(job_id, {
            "id": job_id, "status": "completed", "prompt_id": "old-prompt", "output_type": "video",
            "video_project_id": created["id"], "segment_id": project["segments"][0]["id"], "attempt_id": attempt_id,
            "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "media": MEDIA}],
        })
        restored_fake = FakeComfy(self.settings.comfy_output)
        restored = VideoProjectManager(
            self.settings, self.assets, self.jobs, restored_fake, DEFAULT_REGISTRY, threading.RLock(),
        )
        wait_until(lambda: restored.get(created["id"])["status"] == "completed")
        self.assertEqual(restored_fake.submit_count, 0)
        self.assertEqual(restored.get(created["id"])["segments"][0]["job_id"], job_id)

    def test_merge_validates_media_and_persists_protected_result(self) -> None:
        created = self.create_project(["none", "none"])
        project = self.manager.store.get(created["id"])
        for index, segment in enumerate(project["segments"]):
            job_id = f"{index + 500:032x}"
            output = self.settings.comfy_output / f"merge-source-{index}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"source")
            self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "media": dict(MEDIA)}]})
            segment.update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"merged-output")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"]).get("merged", {}).get("status") == "completed")
        merged = self.manager.get(created["id"])["merged"]
        public_project = self.manager.get(created["id"])
        self.assertTrue(public_project["segments"][0]["thumbnail_url"].startswith("/api/jobs/"))
        self.assertEqual(
            merged["thumbnail_url"],
            f"/api/video-projects/{created['id']}/merged/thumbnail",
        )
        self.assertEqual(merged["sha256"], hashlib.sha256(b"merged-output").hexdigest())
        self.assertEqual(merged["media"]["fps"], 24)
        self.assertIn("concat", commands[0])
        self.assertIn("copy", commands[0])
        self.assertNotIn("filter_complex", commands[0])
        self.assertEqual([source["index"] for source in merged["sources"]], [0, 1])
        self.assertEqual(
            [source["sha256"] for source in merged["sources"]],
            [hashlib.sha256(b"source").hexdigest()] * 2,
        )
        library_job = self.jobs.get(merged["result_job_id"])
        self.assertTrue(library_job["synthetic_merge"])
        self.assertEqual(library_job["outputs"][0]["sha256"], merged["sha256"])
        path, _ = self.manager.merged_path(created["id"])
        self.assertTrue(path.is_relative_to(self.settings.comfy_output.resolve()))

        stored = self.manager.store.get(created["id"])
        stored["merged"]["relative_path"] = "../../outside.mp4"
        self.manager.store.put(created["id"], stored)
        with self.assertRaises(ApiError) as raised:
            self.manager.merged_path(created["id"])
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_merge_rejects_incompatible_dimensions_before_ffmpeg(self) -> None:
        created = self.create_project(["none", "none"])
        project = self.manager.store.get(created["id"])
        for index, segment in enumerate(project["segments"]):
            job_id = f"{index + 700:032x}"
            output = self.settings.comfy_output / f"incompatible-{index}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"source")
            media = dict(MEDIA)
            if index:
                media["width"] = 768
                media["height"] = 1344
            self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "media": media}]})
            segment.update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        called = []
        self.manager.command_runner = lambda *_args, **_kwargs: called.append(True)
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"]).get("merged", {}).get("status") == "failed")
        self.assertIn("same dimensions", self.manager.get(created["id"])["merged"]["error"])
        self.assertEqual(called, [])

    def test_run_one_segment_is_exact_and_project_is_not_falsely_complete(self) -> None:
        created = self.create_project(["none", "none", "none"])
        target = created["segments"][1]["id"]
        self.manager.rerun_segment(created["id"], target)
        wait_until(lambda: self.manager.get(created["id"])["status"] == "partial")
        receipt = self.manager.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 1)
        self.assertEqual([item["status"] for item in receipt["segments"]], ["pending", "completed", "pending"])

    def test_run_selected_segments_spends_only_selected_unfinished_work(self) -> None:
        created = self.create_project(["none", "none", "none"])
        selected = [created["segments"][0]["id"], created["segments"][2]["id"]]
        accepted = self.manager.run(created["id"], selected)
        self.assertEqual(accepted["selected_segment_ids"], selected)
        self.assertEqual(accepted["current_index"], 0)
        wait_until(lambda: self.manager.get(created["id"])["status"] == "partial")
        receipt = self.manager.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 2)
        self.assertEqual(
            [item["status"] for item in receipt["segments"]],
            ["completed", "pending", "completed"],
        )
        self.assertEqual(receipt["selected_segment_ids"], selected)
        self.assertEqual(receipt["current_index"], -1)
        with self.assertRaises(ApiError) as raised:
            self.manager.merge(created["id"])
        self.assertEqual(raised.exception.code, "segments_not_ready")

    def test_selected_run_skips_already_completed_segment(self) -> None:
        created = self.create_project(["none", "none"])
        project = self.manager.store.get(created["id"])
        project["segments"][0]["status"] = "completed"
        self.manager.store.put(created["id"], project)
        selected = [segment["id"] for segment in created["segments"]]
        self.manager.run(created["id"], selected)
        wait_until(lambda: self.manager.get(created["id"])["status"] == "completed")
        self.assertEqual(self.comfy.submit_count, 1)

    def test_selected_run_is_recovered_after_restart_without_expanding_selection(self) -> None:
        created = self.create_project(["none", "none", "none"])
        project = self.manager.store.get(created["id"])
        selected = [project["segments"][2]["id"]]
        project.update({
            "status": "running", "current_index": 2,
            "selected_segment_ids": selected, "stop_requested": False,
        })
        self.manager.store.put(created["id"], project)
        recovered = VideoProjectManager(
            self.settings, self.assets, self.jobs, self.comfy,
            DEFAULT_REGISTRY, threading.RLock(),
        )
        wait_until(lambda: recovered.get(created["id"])["status"] == "partial")
        receipt = recovered.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 1)
        self.assertEqual(
            [item["status"] for item in receipt["segments"]],
            ["pending", "pending", "completed"],
        )
        self.assertEqual(receipt["selected_segment_ids"], selected)

    def test_restart_selected_source_range_runs_only_selected_crop_and_gpu_job(self) -> None:
        source_id = "6" * 32
        self.add_asset(source_id, "video", {**MEDIA, "duration": 20.0, "frame_count": 480})
        ranges = [
            {"asset_id": source_id, "start_frame": 0, "end_frame": 120, "fps": 24.0},
            {"asset_id": source_id, "start_frame": 120, "end_frame": 240, "fps": 24.0},
        ]
        created = self.manager.create({
            "title": "Restart exact source selection",
            "storyboard": {
                "source_asset_id": source_id, "fps": 24.0,
                "frame_count": 480, "cut_frames": [120, 240],
            },
            "segments": [
                {
                    "continuation": "none", "request": self.request("minimax-h3-ref2va"),
                    "source_range": source_range,
                }
                for source_range in ranges
            ],
        })
        project = self.manager.store.get(created["id"])
        selected = [project["segments"][1]["id"]]
        project.update({
            "status": "running", "current_index": 1,
            "selected_segment_ids": selected, "stop_requested": False,
        })
        self.manager.store.put(created["id"], project)
        commands: list[list[str]] = []

        def ffmpeg(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomselected")
            return subprocess.CompletedProcess(command, 0, "", "")

        recovered = VideoProjectManager(
            self.settings, self.assets, self.jobs, self.comfy,
            DEFAULT_REGISTRY, threading.RLock(), command_runner=ffmpeg,
        )
        wait_until(lambda: recovered.get(created["id"])["status"] == "partial")
        receipt = recovered.get(created["id"])
        self.assertEqual(self.comfy.submit_count, 1)
        self.assertEqual([item["status"] for item in receipt["segments"]], ["pending", "completed"])
        self.assertEqual(receipt["selected_segment_ids"], selected)
        self.assertEqual(len(commands), 1)
        self.assertIn("trim=start_frame=120:end_frame=240", commands[0][commands[0].index("-vf") + 1])

    def test_selected_run_validates_ids_and_auto_includes_continuation_dependencies(self) -> None:
        created = self.create_project(["none", "tail_frame"])
        first, second = [segment["id"] for segment in created["segments"]]
        for value, code in (
            ([], "invalid_segment_ids"),
            ([first, first], "invalid_segment_ids"),
            (["f" * 32], "segment_not_found"),
        ):
            with self.subTest(value=value), self.assertRaises(ApiError) as raised:
                self.manager.run(created["id"], value)
            self.assertEqual(raised.exception.code, code)
        with patch.object(self.manager, "_start"):
            receipt = self.manager.run(created["id"], [second])
        self.assertEqual(receipt["selected_segment_ids"], [first, second])
        self.assertEqual(receipt["current_index"], 0)
        self.assertEqual(self.comfy.submit_count, 0)

    def test_selected_run_cost_property_matches_selected_unfinished_set(self) -> None:
        rng = random.Random(1313)
        for case in range(8):
            with self.subTest(case=case):
                created = self.create_project(["none"] * 6)
                project = self.manager.store.get(created["id"])
                initially_completed = {
                    index for index in range(6) if rng.random() < 0.35
                }
                for index in initially_completed:
                    project["segments"][index]["status"] = "completed"
                self.manager.store.put(created["id"], project)

                selected_indices = {
                    index for index in range(6) if rng.random() < 0.55
                } or {rng.randrange(6)}
                # Reverse request order to prove cost and execution membership
                # do not depend on the caller's array ordering.
                selected_ids = [
                    created["segments"][index]["id"]
                    for index in sorted(selected_indices, reverse=True)
                ]
                expected_paid = selected_indices - initially_completed
                before_submits = self.comfy.submit_count
                receipt = self.manager.run(created["id"], selected_ids)
                if expected_paid:
                    wait_until(lambda: self.manager.get(created["id"])["status"] in {"partial", "completed"})
                    receipt = self.manager.get(created["id"])
                self.assertEqual(self.comfy.submit_count - before_submits, len(expected_paid))
                self.assertEqual(
                    receipt["selected_segment_ids"],
                    [created["segments"][index]["id"] for index in sorted(selected_indices)],
                )
                for index, segment in enumerate(receipt["segments"]):
                    if index in initially_completed or index in expected_paid:
                        self.assertEqual(segment["status"], "completed")
                    else:
                        self.assertEqual(segment["status"], "pending")
                    self.assertEqual(
                        len(segment["attempts"]),
                        1 if index in expected_paid else 0,
                    )

    def test_continuation_selection_is_auto_expanded_to_dependency_closure(self) -> None:
        created = self.create_project(["none", "tail_frame", "tail_frame", "tail_frame"])
        ids = [segment["id"] for segment in created["segments"]]
        with patch.object(self.manager, "_start"):
            receipt = self.manager.run(created["id"], [ids[2]])
        self.assertEqual(receipt["selected_segment_ids"], ids[:3])
        self.assertEqual(receipt["current_index"], 0)
        self.assertEqual(self.comfy.submit_count, 0)

    def test_previous_video_is_fully_preflighted_before_any_paid_work(self) -> None:
        invalid = self.request("minimax-h3-ref2va")
        invalid["parameters"]["steps"] = 999
        with self.assertRaises(ApiError) as raised:
            self.create_project(["none", "previous_video"], [self.request(), invalid])
        self.assertEqual(raised.exception.code, "invalid_parameter")
        self.assertEqual(self.comfy.submit_count, 0)

    def test_continuation_rejects_literal_modality_tags_in_prompt_and_parts(self) -> None:
        for field, value in (
            ("prompt", "Continue from <Picture 1>"),
            ("parts", {"camera": "Match <Video 2> movement"}),
        ):
            request = self.request()
            request[field] = value
            with self.subTest(field=field), self.assertRaises(ApiError) as raised:
                self.create_project(["none", "tail_frame"], [self.request(), request])
            self.assertEqual(raised.exception.code, "unstable_reference_tag")

    def test_tail_prompt_binds_derived_and_custom_endpoint_by_stable_alias(self) -> None:
        endpoint_id = "1" * 32
        self.add_asset(endpoint_id, "image")
        request = self.request()
        request["prompt"] = f"End with the composition from @{{{endpoint_id}}}"
        request["references"] = [{"asset_id": endpoint_id, "role": "last_frame"}]
        created = self.create_project(["none", "tail_frame"], [self.request(), request])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "stable-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source video")
        job_id = "2" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})

        def ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x89PNG\r\n\x1a\nframe")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = ffmpeg
        prepared, _ = self.manager._prepare_request(project, 1)
        spec = parse_generation_request({**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(spec.prompt, "End with the composition from <Picture 2>; <Picture 1>")
        self.assertNotIn("continue seamlessly", spec.prompt)

    def test_preserve_tags_only_survives_project_validation_and_continuation_without_hidden_semantics(self) -> None:
        picture_id = "4" * 32
        previous_id = "5" * 32
        self.add_asset(picture_id, "image")
        self.add_asset(previous_id, "video", {
            "duration": 5.0, "fps": 24.0, "reference_fps": 24.0,
            "has_video": True, "has_audio": True,
        })
        request = self.request("minimax-h3-ref2va")
        request.update({
            "prompt": f"Keep @{{{picture_id}}} while the action continues",
            "parts": {},
            "prompt_mode": "preserve_tags_only",
            "references": [{"asset_id": picture_id, "role": "identity"}],
        })
        created = self.create_project(["none", "previous_video"], [self.request(), request])
        stored = self.manager.store.get(created["id"])["segments"][1]["request"]
        self.assertEqual(stored["prompt_mode"], "preserve_tags_only")
        prepared = self.manager._with_continuation_reference(stored, "previous_video", previous_id)
        spec = parse_generation_request({**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        expected = "Keep <Picture 1> while the action continues; <Video 1>"
        self.assertEqual(spec.prompt, expected)
        workflow = compile_video_workflow(spec, self.settings, "6" * 32)
        self.assertEqual(workflow["8"]["inputs"]["prompt"], expected)
        self.assertNotIn("subject_definitions", spec.prompt)
        self.assertNotIn("preserve the target identity", spec.prompt)

    def test_long_video_execution_forces_read_only_prompt_mode_for_legacy_projects(self) -> None:
        created = self.create_project(["none"], [self.request()])
        stored = self.manager.store.get(created["id"])["segments"][0]["request"]
        self.assertNotIn("prompt_mode", stored)
        self.assertEqual(stored["parts"], {"camera": "steady medium tracking shot"})

        prepared, evidence = self.manager._prepare_request(self.manager.store.get(created["id"]), 0)
        self.assertEqual(evidence, {"mode": "none"})
        self.assertEqual(prepared["prompt_mode"], "preserve_tags_only")
        self.assertEqual(prepared["parts"], {})
        spec = parse_generation_request({**prepared, "output_type": "video"}, self.assets.get, DEFAULT_REGISTRY)
        self.assertEqual(spec.prompt, "A cream robot continues walking through a living room")
        self.assertNotIn("steady medium tracking shot", spec.prompt)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_real_h264_aac_tail_extraction_decodes_true_last_pixel(self) -> None:
        created = self.create_project(["none", "tail_frame"])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "real-tail-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=red:s=64x64:r=24:d=0.5",
            "-f", "lavfi", "-i", "color=blue:s=64x64:r=24:d=0.5",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ]
        subprocess.run(command, check=True, capture_output=True)
        job_id = "3" * 32
        tail_media = {**MEDIA, "duration": 1.0, "video_duration": 1.0, "frame_count": 24}
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": tail_media}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        prepared, evidence = self.manager._prepare_request(project, 1)
        tail = self.assets.content_path(self.assets.get(evidence["asset_id"]))
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(tail), "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(len(decoded), 3)
        self.assertGreater(decoded[2], decoded[0] + 100)
        self.assertEqual(prepared["references"][0]["role"], "first_frame")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_tail_extraction_uses_video_duration_when_audio_outlasts_picture(self) -> None:
        created = self.create_project(["none", "tail_frame"])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "audio-outlasts-video.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=blue:s=64x64:r=24:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-t", "3", str(source),
        ], check=True, capture_output=True)
        job_id = "9" * 32
        media = {**MEDIA, "duration": 3.0, "video_duration": 1.0, "frame_count": 24}
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": media,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        _, evidence = self.manager._prepare_request(project, 1)
        tail = self.assets.content_path(self.assets.get(evidence["asset_id"]))
        self.assertTrue(tail.is_file())
        self.assertGreater(tail.stat().st_size, 0)

    def test_continuation_rejects_missing_recorded_source_hash(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "missing-hash-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "1" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        with self.assertRaises(ApiError) as raised:
            self.manager._prepare_request(project, 1)
        self.assertEqual(raised.exception.code, "output_integrity")

    def test_restart_stopping_cancels_prompt_and_terminalizes_attempt(self) -> None:
        self.comfy.complete = False
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        attempt_id, job_id = "4" * 32, "5" * 32
        project.update({"status": "stopping", "current_index": 0, "stop_requested": True})
        project["segments"][0].update({"status": "running", "job_id": job_id, "attempts": [{"id": attempt_id, "status": "queued", "job_id": job_id, "continuation": {"mode": "none"}}]})
        self.manager.store.put(created["id"], project)
        self.jobs.put(job_id, {"id": job_id, "status": "queued", "prompt_id": "live-prompt", "video_project_id": created["id"], "segment_id": project["segments"][0]["id"], "attempt_id": attempt_id})
        restored_fake = FakeComfy(self.settings.comfy_output, complete=False)
        restored = VideoProjectManager(self.settings, self.assets, self.jobs, restored_fake, DEFAULT_REGISTRY, threading.RLock())
        receipt = restored.get(created["id"])
        self.assertEqual(restored_fake.canceled, ["live-prompt"])
        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["segments"][0]["attempts"][0]["status"], "canceled")
        self.assertEqual(self.jobs.get(job_id)["status"], "canceled")

    def test_restart_stopping_cancels_running_prompt(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        attempt_id, job_id = "e" * 32, "f" * 32
        project.update({"status": "stopping", "current_index": 0, "stop_requested": True})
        project["segments"][0].update({"status": "running", "job_id": job_id, "attempts": [{"id": attempt_id, "status": "running", "job_id": job_id, "continuation": {"mode": "none"}}]})
        self.manager.store.put(created["id"], project)
        self.jobs.put(job_id, {"id": job_id, "status": "running", "prompt_id": "running-prompt", "video_project_id": created["id"], "segment_id": project["segments"][0]["id"], "attempt_id": attempt_id})
        restored_fake = FakeComfy(self.settings.comfy_output, complete=False)
        restored = VideoProjectManager(self.settings, self.assets, self.jobs, restored_fake, DEFAULT_REGISTRY, threading.RLock())
        self.assertEqual(restored_fake.canceled, ["running-prompt"])
        self.assertEqual(restored.get(created["id"])["status"], "stopped")
        self.assertEqual(self.jobs.get(job_id)["status"], "canceled")

    def test_old_unverifiable_submitting_job_terminal_fails_without_resubmit(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        attempt_id, job_id = "6" * 32, "7" * 32
        project.update({"status": "running", "current_index": 0})
        project["segments"][0].update({"status": "running", "job_id": job_id, "attempts": [{"id": attempt_id, "status": "submitting", "job_id": job_id, "continuation": {"mode": "none"}}]})
        self.manager.store.put(created["id"], project)
        self.jobs.put(job_id, {"id": job_id, "status": "submitting", "client_id": "lost-client", "created_at": 1, "submission_started_at": 1, "video_project_id": created["id"], "segment_id": project["segments"][0]["id"], "attempt_id": attempt_id})
        restored_fake = FakeComfy(self.settings.comfy_output)
        restored = VideoProjectManager(self.settings, self.assets, self.jobs, restored_fake, DEFAULT_REGISTRY, threading.RLock())
        wait_until(lambda: restored.get(created["id"])["status"] == "failed")
        self.assertEqual(restored_fake.submit_count, 0)
        self.assertEqual(self.jobs.get(job_id)["status"], "failed")

    def test_completed_history_recovers_submitting_job_without_resubmit(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        attempt_id, job_id = "1" * 32, "2" * 32
        project.update({"status": "running", "current_index": 0})
        project["segments"][0].update({"status": "running", "job_id": job_id, "attempts": [{"id": attempt_id, "status": "submitting", "job_id": job_id, "continuation": {"mode": "none"}}]})
        self.manager.store.put(created["id"], project)
        self.jobs.put(job_id, {"id": job_id, "status": "submitting", "client_id": "history-client", "created_at": time.time(), "submission_started_at": time.time(), "video_project_id": created["id"], "segment_id": project["segments"][0]["id"], "attempt_id": attempt_id})
        output = self.settings.comfy_output / "history-recovered.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"history output")
        class HistoryFake(FakeComfy):
            def find_prompt_by_client_id(self, client_id):
                return "history-prompt" if client_id == "history-client" else None
        restored_fake = HistoryFake(self.settings.comfy_output)
        restored_fake.records["history-prompt"] = {"outputs": {"20": {"videos": [{"filename": output.name, "subfolder": "", "type": "output"}]}}}
        restored = VideoProjectManager(self.settings, self.assets, self.jobs, restored_fake, DEFAULT_REGISTRY, threading.RLock())
        wait_until(lambda: restored.get(created["id"])["status"] == "completed")
        self.assertEqual(restored_fake.submit_count, 0)
        self.assertEqual(self.jobs.get(job_id)["prompt_id"], "history-prompt")

    def test_merge_recomputes_source_hash_and_rejects_replacement(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "8" * 32
        source = self.settings.comfy_output / "replaced.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"replacement")
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(b"original").hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        called = []
        self.manager.command_runner = lambda *_args, **_kwargs: called.append(True)
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "failed")
        self.assertEqual(called, [])
        self.assertIn("recorded hash", self.manager.get(created["id"])["merged"]["error"])

    def test_stop_during_merge_cancels_and_removes_partial_staging(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "9" * 32
        source = self.settings.comfy_output / "cancel-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        started, release = threading.Event(), threading.Event()
        durable_before_command: list[bool] = []
        def blocking(command, **_kwargs):
            active = self.manager.store.get(created["id"])["merge_attempts"][-1]
            durable_before_command.append(bool(active.get("staging_relative_path")))
            started.set()
            release.wait(2)
            Path(command[-1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 0, "", "")
        self.manager.command_runner = blocking
        self.manager.merge(created["id"])
        self.assertTrue(started.wait(2))
        self.manager.stop(created["id"])
        release.set()
        wait_until(lambda: self.manager.get(created["id"])["status"] == "stopped")
        stored = self.manager.store.get(created["id"])
        attempt = stored["merge_attempts"][-1]
        self.assertEqual(attempt["status"], "canceled")
        self.assertEqual(durable_before_command, [True])
        staging = secure_join(self.settings.comfy_output, attempt["staging_relative_path"])
        self.assertFalse(staging.exists())

    def test_immediate_merge_stop_is_not_lost_before_worker_starts(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "7" * 32
        source = self.settings.comfy_output / "immediate-stop-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        with patch.object(self.manager, "_start"):
            self.manager.merge(created["id"])
        attempt_id = self.manager.store.get(created["id"])["merge_attempts"][-1]["id"]
        self.manager.stop(created["id"])
        called = []
        self.manager.command_runner = lambda *_args, **_kwargs: called.append(True)
        self.manager._merge_project(created["id"], attempt_id)
        receipt = self.manager.get(created["id"])
        self.assertEqual(receipt["status"], "stopped")
        self.assertEqual(receipt["merged"]["status"], "canceled")
        self.assertEqual(called, [])

    def test_restart_cleans_persisted_merge_staging_and_destination(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        staging = self.settings.comfy_output / "h3-studio/projects" / created["id"] / ".partial.mp4"
        destination = staging.with_name("merged-orphan.mp4")
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"partial")
        destination.write_bytes(b"promoted-before-metadata")
        project["status"] = "merging"
        project["merge_attempts"] = [{
            "id": "a" * 32, "status": "merging",
            "staging_relative_path": staging.relative_to(self.settings.comfy_output).as_posix(),
            "destination_relative_path": destination.relative_to(self.settings.comfy_output).as_posix(),
        }]
        self.manager.store.put(created["id"], project)
        restored = VideoProjectManager(self.settings, self.assets, self.jobs, FakeComfy(self.settings.comfy_output), DEFAULT_REGISTRY, threading.RLock())
        self.assertEqual(restored.get(created["id"])["status"], "failed")
        self.assertFalse(staging.exists())
        self.assertFalse(destination.exists())

    def test_delete_project_reclaims_owned_derived_asset_but_keeps_source(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "delete-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "b" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()
        _, evidence = self.manager._prepare_request(project, 1)
        project["segments"][1]["attempts"] = [{"id": "c" * 32, "status": "failed", "continuation": evidence}]
        self.manager.store.put(created["id"], project)
        result = self.manager.delete(created["id"])
        self.assertEqual(result["reclaimed_derived_assets"], 1)
        with self.assertRaises(ApiError):
            self.assets.get(evidence["asset_id"])
        self.assertTrue(source.exists())

    def test_reconcile_reclaims_continuation_imported_before_attempt_commit(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "crash-window-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "e" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.manager.store.put(created["id"], project)
        self.stub_video_only_ffmpeg()
        _, evidence = self.manager._prepare_request(project, 1)
        self.assertEqual(self.assets.get(evidence["asset_id"])["derived"]["project_id"], created["id"])
        # Simulate a hard crash before attempt.continuation is durably stored.
        self.manager.reconcile()
        with self.assertRaises(ApiError):
            self.assets.get(evidence["asset_id"])

    def test_delete_reclaims_unattached_owned_continuation_asset(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        temp = self.settings.data_root / "tmp" / "unattached.mp4"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(b"\x00\x00\x00\x18ftypisomorphan")
        with patch.object(AssetStore, "_probe_media", return_value=MEDIA):
            asset = self.manager._import_continuation_asset(
                temp, original_filename="orphan.mp4", requested_kind="video",
                claimed_content_type="video/mp4", project_id=created["id"],
                segment_id=project["segments"][1]["id"], source_sha256="a" * 64,
            )
        result = self.manager.delete(created["id"])
        self.assertEqual(result["reclaimed_derived_assets"], 1)
        with self.assertRaises(ApiError):
            self.assets.get(asset["id"])

    def test_delete_project_preserves_derived_asset_reused_by_other_project(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "shared-derived-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "3" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()
        _, evidence = self.manager._prepare_request(project, 1)
        project["segments"][1]["attempts"] = [{"id": "4" * 32, "status": "failed", "continuation": evidence}]
        self.manager.store.put(created["id"], project)
        reused = self.request("minimax-h3-ref2va")
        reused["references"] = [{"asset_id": evidence["asset_id"], "role": "motion", "include_audio": False}]
        self.create_project(["none"], [reused])
        result = self.manager.delete(created["id"])
        self.assertEqual(result["reclaimed_derived_assets"], 0)
        self.assertEqual(self.assets.get(evidence["asset_id"])["derived"]["project_id"], created["id"])

    def test_rerun_reclaims_superseded_derived_binary_and_recovers_quota(self) -> None:
        created = self.create_project(["none", "previous_video"], [self.request(), self.request("minimax-h3-ref2va")])
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "superseded-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "5" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()
        _, evidence = self.manager._prepare_request(project, 1)
        project["segments"][1].update({"status": "failed", "attempts": [{"id": "6" * 32, "status": "failed", "continuation": evidence}]})
        self.manager.store.put(created["id"], project)
        before = self.assets.used_bytes()
        with patch.object(self.manager, "_start"):
            self.manager.rerun_segment(created["id"], project["segments"][1]["id"])
        after = self.assets.used_bytes()
        self.assertLess(after, before)
        continuation = self.manager.get(created["id"])["segments"][1]["attempts"][0]["continuation"]
        self.assertIn("asset_reclaimed_at", continuation)
        self.assertEqual(continuation["source_sha256"], evidence["source_sha256"])

    def test_rerun_preserves_derived_asset_referenced_by_same_project_segment(self) -> None:
        created = self.create_project(
            ["none", "previous_video", "none"],
            [self.request(), self.request("minimax-h3-ref2va"), self.request()],
        )
        project = self.manager.store.get(created["id"])
        source = self.settings.comfy_output / "same-project-derived-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
        job_id = "6" * 32
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
            "filename": source.name, "subfolder": "", "type": "output",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
        }]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        self.stub_video_only_ffmpeg()
        _, evidence = self.manager._prepare_request(project, 1)
        project["segments"][1]["attempts"] = [{"id": "7" * 32, "status": "completed", "continuation": evidence}]
        project["segments"][2]["request"]["references"] = [{"asset_id": evidence["asset_id"], "role": "motion", "include_audio": False}]
        self.manager.store.put(created["id"], project)
        self.assertEqual(self.manager._reclaim_segment_assets(project, [1]), 0)
        self.assertEqual(self.assets.get(evidence["asset_id"])["id"], evidence["asset_id"])

    def test_scalable_merge_uses_one_concat_demuxer_input_for_many_segments(self) -> None:
        count = 40
        created = self.create_project(["none"] * count)
        project = self.manager.store.get(created["id"])
        for index, segment in enumerate(project["segments"]):
            job_id = f"{index + 1000:032x}"
            source = self.settings.comfy_output / f"scale-{index}.mp4"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"source-{index}".encode())
            self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
            segment.update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        observed: dict[str, object] = {}
        def ffmpeg(command, **_kwargs):
            observed["command"] = command
            concat = Path(command[command.index("-i") + 1])
            observed["concat"] = concat.read_text(encoding="utf-8")
            Path(command[-1]).write_bytes(b"merged")
            return subprocess.CompletedProcess(command, 0, "", "")
        self.manager.command_runner = ffmpeg
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "completed")
        command = observed["command"]
        self.assertIsInstance(command, list)
        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(str(observed["concat"]).count("\nfile '"), count)

    def test_merge_quota_is_checked_before_ffmpeg(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "d" * 32
        source = self.settings.comfy_output / "quota-source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        self.manager.config = replace(self.settings, max_merged_output_bytes=1)
        called = []
        self.manager.command_runner = lambda *_args, **_kwargs: called.append(True)
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "failed")
        self.assertEqual(called, [])
        self.assertIn("quota", self.manager.get(created["id"])["merged"]["error"])

    def test_concurrent_merge_quota_check_and_reservation_are_atomic(self) -> None:
        projects = []
        for number in range(2):
            created = self.create_project(["none"])
            project = self.manager.store.get(created["id"])
            job_id = f"{number + 9000:032x}"
            source = self.settings.comfy_output / f"atomic-quota-{number}.mp4"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"x")
            self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{
                "filename": source.name, "subfolder": "", "type": "output",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": MEDIA,
            }]})
            project["segments"][0].update({"status": "completed", "job_id": job_id})
            project["status"] = "completed"
            self.manager.store.put(created["id"], project)
            projects.append(created)
        self.manager.config = replace(self.settings, max_merged_output_bytes=20 * 1024 * 1024)
        entered = threading.Event()
        release = threading.Event()

        def blocked(command, **_kwargs):
            entered.set()
            release.wait(2)
            Path(command[-1]).write_bytes(b"merged")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.manager.command_runner = blocked
        self.manager.merge(projects[0]["id"])
        self.assertTrue(entered.wait(1))
        self.manager.merge(projects[1]["id"])
        wait_until(lambda: self.manager.get(projects[1]["id"])["status"] == "failed")
        self.assertIn("quota", self.manager.get(projects[1]["id"])["merged"]["error"])
        release.set()
        wait_until(lambda: self.manager.get(projects[0]["id"])["status"] == "completed")

    def test_merge_rejects_source_without_recorded_hash(self) -> None:
        created = self.create_project(["none"])
        project = self.manager.store.get(created["id"])
        job_id = "a" * 32
        source = self.settings.comfy_output / "merge-missing-hash.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "media": MEDIA}]})
        project["segments"][0].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        self.manager.merge(created["id"])
        wait_until(lambda: self.manager.get(created["id"])["status"] == "failed")
        self.assertIn("recorded hash", self.manager.get(created["id"])["merged"]["error"])

    @unittest.skipUnless(os.name == "posix" and Path("/bin/sh").is_file(), "POSIX process groups are required")
    def test_merge_cancel_terminates_spawned_descendant_process_group(self) -> None:
        output = self.root / "group-output.mp4"
        child_pid_file = self.root / "child.pid"
        cancel = threading.Event()
        errors: list[Exception] = []
        command = [
            "/bin/sh", "-c",
            f"sleep 30 & echo $! > '{child_pid_file}'; printf x > '{output}'; wait",
        ]

        def run() -> None:
            try:
                self.manager._run_merge_command("f" * 32, command, output, 60, cancel, 30)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        wait_until(child_pid_file.is_file)
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        cancel.set()
        worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(error.__class__.__name__ == "MergeCanceled" for error in errors))
        deadline = time.time() + 3
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        if alive:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        self.assertFalse(alive, "merge descendant survived process-group cancellation")

    @unittest.skipUnless(os.name == "posix" and shutil.which("python3"), "POSIX process groups are required")
    def test_process_group_kills_descendant_that_ignores_sigterm(self) -> None:
        child_pid_file = self.root / "stubborn-child.pid"
        script = (
            "import os,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(child_pid_file)!r},'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )
        process = subprocess.Popen(
            ["/bin/sh", "-c", f"{shutil.which('python3')} -c {script!r} & wait"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        wait_until(child_pid_file.is_file)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.manager._terminate_process_group(process, grace_seconds=0.2)
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.fail("SIGTERM-ignoring merge descendant survived process-group cancellation")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_real_concat_demuxer_merge_preserves_h264_aac_and_order(self) -> None:
        created = self.create_project(["none", "none"])
        project = self.manager.store.get(created["id"])
        for index, color in enumerate(("red", "blue")):
            source = self.settings.comfy_output / f"real-merge-{index}.mp4"
            source.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=64x64:r=24:d=0.5",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=0.5", "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ], check=True, capture_output=True)
            job_id = f"{index + 2000:032x}"
            self.probe.stop()
            try:
                actual_media = AssetStore._probe_media(source, "video")
            finally:
                self.probe.start()
            self.jobs.put(job_id, {"id": job_id, "status": "completed", "outputs": [{"filename": source.name, "subfolder": "", "type": "output", "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "media": actual_media}]})
            project["segments"][index].update({"status": "completed", "job_id": job_id})
        project["status"] = "completed"
        self.manager.store.put(created["id"], project)
        self.manager.command_runner = subprocess.run
        # Temporarily use the real probe rather than the class-level fixture.
        self.probe.stop()
        try:
            self.manager.merge(created["id"])
            wait_until(lambda: self.manager.get(created["id"])["status"] in {"completed", "failed"}, timeout=10)
            receipt = self.manager.get(created["id"])
            self.assertEqual(receipt["status"], "completed", receipt.get("merged"))
            merged_path, _ = self.manager.merged_path(created["id"])
            media = AssetStore._probe_media(merged_path, "video")
            self.assertEqual(media["video_codec"], "h264")
            self.assertEqual(media["audio_codec"], "aac")
            self.assertAlmostEqual(media["fps"], 24.0, places=2)
            self.assertGreater(media["duration"], 0.8)
            self.assertLess(media["duration"], 1.2)

            def sampled_rgb(position: float) -> tuple[float, float, float]:
                decoded = subprocess.run([
                    "ffmpeg", "-v", "error", "-ss", str(position),
                    "-i", str(merged_path), "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
                ], check=True, capture_output=True).stdout
                channels = [decoded[offset::3] for offset in range(3)]
                return tuple(sum(channel) / len(channel) for channel in channels)

            first_rgb = sampled_rgb(0.1)
            last_rgb = sampled_rgb(0.8)
            self.assertGreater(first_rgb[0], first_rgb[2] + 100, first_rgb)
            self.assertGreater(last_rgb[2], last_rgb[0] + 100, last_rgb)
        finally:
            self.probe.start()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    def test_character_migration_finalizer_exact_frames_and_all_audio_policies(self) -> None:
        source_id, character_id = "6" * 32, "7" * 32
        source_path = self.settings.comfy_input / "h3-studio" / f"{source_id}.mp4"
        character_path = self.settings.comfy_input / "h3-studio" / f"{character_id}.png"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=red:s=64x64:r=24:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source_path),
        ], check=True, capture_output=True)
        character_path.write_bytes(b"png-placeholder")
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        character_sha = hashlib.sha256(character_path.read_bytes()).hexdigest()
        source = {
            "id": source_id, "kind": "video", "filename": source_path.name,
            "stored_name": source_path.name, "comfy_path": f"h3-studio/{source_path.name}",
            "sha256": source_sha, "storage_size": source_path.stat().st_size,
            "media": {
                "duration": 3.0, "video_duration": 3.0, "width": 1024, "height": 1024,
                "fps": 24.0, "reference_fps": 24.0, "frame_count": 72, "has_audio": True,
            },
        }
        character = {
            "id": character_id, "kind": "image", "filename": character_path.name,
            "stored_name": character_path.name, "comfy_path": f"h3-studio/{character_path.name}",
            "sha256": character_sha, "storage_size": character_path.stat().st_size,
            "media": {"width": 64, "height": 64},
        }
        self.assets.metadata.put(source_id, source)
        self.assets.metadata.put(character_id, character)
        self.manager.command_runner = subprocess.run
        for audio_policy in ("copy-source", "reference-source", "generate", "mute"):
            with self.subTest(audio_policy=audio_policy):
                planned = plan_migration(
                    {
                        "version": MIGRATION_VERSION, "source_asset_id": source_id,
                        "targets": [{"character_asset_id": character_id, "source_subject": "the centered dancer"}],
                        "segment_frames": 124, "overlap_frames": 5,
                        "audio_policy": audio_policy,
                    },
                    source=source, character=character, registry=DEFAULT_REGISTRY,
                )
                created = self.manager.create(planned["project"])
                attempt_id = f"attempt-{audio_policy}"
                stored = self.manager.store.get(created["id"])
                stored["merge_attempts"] = [{"id": attempt_id, "status": "merging"}]
                self.manager.store.put(created["id"], stored)
                concatenated = self.settings.comfy_output / f"concat-{audio_policy}.mp4"
                concatenated.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=blue:s=64x64:r=24:d=5.166666667",
                    "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=5.166666667",
                    "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(concatenated),
                ], check=True, capture_output=True)
                finalized, receipt = self.manager._finalize_character_migration(
                    stored, concatenated, (1024, 1024), attempt_id, threading.Event(),
                )
                self.probe.stop()
                try:
                    media = AssetStore._probe_media(finalized, "video")
                finally:
                    self.probe.start()
                self.assertEqual(media["frame_count"], 72)
                self.assertAlmostEqual(media["duration"], 3.0, places=3)
                self.assertEqual(media["has_audio"], audio_policy != "mute")
                self.assertEqual(receipt["audio_policy"], audio_policy)


if __name__ == "__main__":
    unittest.main()
