from __future__ import annotations

import tempfile
import threading
import time
import unittest
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.errors import ApiError
from server.gpu_resources import GpuResourceManager
from server.tests.test_workflows import config as base_config
from server.voice import ProcessVoiceWorker, VoiceTaskManager, voice_capability, yingmusic_parameters
from server.voice_worker import _prepend_sys_path


class FakeAssets:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.values = {}
        for identifier, filename in (("a" * 32, "source.wav"), ("b" * 32, "reference.wav")):
            path = root / filename
            path.write_bytes(b"RIFFfake-wave")
            self.values[identifier] = {"id": identifier, "kind": "audio", "path": path}

    def get(self, asset_id: str):
        try:
            return self.values[asset_id]
        except KeyError as error:
            raise ApiError(404, "not_found", "asset missing") from error

    @staticmethod
    def content_path(asset):
        return asset["path"]

    @staticmethod
    def hash_file(path: Path) -> str:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self.block = False
        self.started = threading.Event()
        self.stops = 0

    def run(self, engine, request, cancel):
        self.calls.append(engine)
        self.requests.append(request)
        self.started.set()
        while self.block and not cancel.wait(0.01):
            pass
        if cancel.is_set():
            raise ApiError(409, "voice_canceled", "canceled")
        Path(request["output"]).write_bytes(b"RIFFconverted")
        if request.get("output_options", {}).get("include_stems"):
            Path(request["output"]).with_name("dry-vocal.wav").write_bytes(b"RIFFdry")
            Path(request["output"]).with_name("accompaniment.wav").write_bytes(b"RIFFaccompaniment")
        return {"ok": True}

    def stop(self):
        self.stops += 1

    @staticmethod
    def status():
        return {"running": False, "engine": None, "pid": None}


class VoiceTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        repo = root / "Amphion"
        script = repo / "models" / "svc" / "vevo2" / "infer_vevo2_fm.py"
        script.parent.mkdir(parents=True)
        script.write_text("# test", encoding="utf-8")
        python = root / "python"
        python.write_text("", encoding="utf-8")
        self.config = replace(
            base_config(root),
            vevo2_root=str(repo), vevo2_python=str(python),
            max_active_voice_tasks=4,
        )
        self.config.prepare()
        self.assets = FakeAssets(root)
        self.resources = GpuResourceManager(idle_release_seconds=0, memory_probe=lambda _i: {})
        self.worker = FakeWorker()
        self.manager = VoiceTaskManager(self.config, self.assets, self.resources, self.worker)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.manager.stop()
        self.resources.stop()
        self.temp.cleanup()

    def wait_terminal(self, task_id: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = self.manager.get(task_id)
            if task["status"] in {"completed", "failed", "canceled"}:
                return task
            time.sleep(0.01)
        self.fail("voice task did not finish")

    def test_submit_runs_on_gpu_queue_and_persists_downloadable_output(self) -> None:
        submitted = self.manager.submit({
            "engine": "vevo2", "source_asset_id": "a" * 32,
            "reference_asset_id": "b" * 32, "request_id": "c" * 32,
        })
        completed = self.wait_terminal(submitted["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["output"]["mime_type"], "audio/wav")
        self.assertTrue(self.manager.output_path(submitted["id"]).is_file())
        self.assertEqual(self.worker.calls, ["vevo2"])

    def test_idempotency_replays_same_payload_and_rejects_conflict(self) -> None:
        body = {"engine": "vevo2", "source_asset_id": "a" * 32, "reference_asset_id": "b" * 32, "request_id": "d" * 32}
        first = self.manager.submit(body)
        replay = self.manager.submit(body)
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["idempotent_replay"])
        with self.assertRaises(ApiError) as raised:
            self.manager.submit({**body, "source_asset_id": "b" * 32})
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_yingmusic_parameters_seed_and_idempotency(self) -> None:
        body = {"engine": "yingmusic", "source_asset_id": "a" * 32, "reference_asset_id": "b" * 32,
                "request_id": "9" * 32, "diffusion_steps": 75, "inference_cfg_rate": 0.9, "seed": -1}
        with patch("server.voice.voice_capability", return_value={"available": True}):
            first = self.manager.submit(body)
            replay = self.manager.submit(body)
            self.assertEqual(first["id"], replay["id"])
            self.assertEqual(first["parameters"], replay["parameters"])
            self.assertTrue(0 <= first["parameters"]["seed"] <= 4294967295)
            self.assertEqual(self.wait_terminal(first["id"])["status"], "completed")
            self.assertEqual(self.worker.requests[0]["parameters"], first["parameters"])
            with self.assertRaises(ApiError) as raised:
                self.manager.submit({**body, "diffusion_steps": 100})
            self.assertEqual(raised.exception.code, "idempotency_conflict")
            second = self.manager.submit({**body, "request_id": "8" * 32, "seed": 42})
            self.assertEqual(second["parameters"]["seed"], 42)

    def test_yingmusic_defaults_and_parameter_validation(self) -> None:
        requested, effective = yingmusic_parameters({})
        self.assertEqual(requested, {"diffusion_steps": 100, "inference_cfg_rate": 0.7, "seed": -1})
        self.assertTrue(0 <= effective["seed"] <= 4294967295)
        for value in ({"diffusion_steps": True}, {"diffusion_steps": 9}, {"inference_cfg_rate": float("nan")},
                      {"inference_cfg_rate": 2.1}, {"seed": 4294967296}, {"seed": 1.5}):
            with self.subTest(value=value), self.assertRaises(ApiError):
                yingmusic_parameters(value)
        with self.assertRaises(ApiError):
            self.manager.submit({"engine": "vevo2", "source_asset_id": "a" * 32,
                                 "reference_asset_id": "b" * 32, "seed": 42})

    def test_yingmusic_optional_tracks_and_effects_are_durable_and_idempotent(self) -> None:
        body = {"engine": "yingmusic", "source_asset_id": "a" * 32, "reference_asset_id": "b" * 32,
                "request_id": "7" * 32, "output_options": {"include_stems": True, "echo": False, "reverb": False}}
        with patch("server.voice.voice_capability", return_value={"available": True}):
            submitted = self.manager.submit(body)
            finished = self.wait_terminal(submitted["id"])
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(self.worker.requests[0]["output_options"], body["output_options"])
            self.assertEqual(set(finished["outputs"]), {"mix", "dry_vocal", "accompaniment"})
            self.assertEqual(self.manager.output_path(submitted["id"], "dry_vocal").read_bytes(), b"RIFFdry")
            self.assertEqual(self.manager.output_path(submitted["id"], "accompaniment").read_bytes(), b"RIFFaccompaniment")
            self.assertEqual(self.manager.get(submitted["id"])["outputs"], finished["outputs"])
            self.assertTrue(self.manager.submit(body)["idempotent_replay"])
            with self.assertRaises(ApiError) as conflict:
                self.manager.submit({**body, "output_options": {**body["output_options"], "echo": True}})
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
            self.assertEqual(self.manager.output_path(submitted["id"]).read_bytes(), b"RIFFconverted")
            with self.assertRaises(ApiError) as invalid_track:
                self.manager.output_path(submitted["id"], "../dry_vocal")
            self.assertEqual(invalid_track.exception.code, "voice_track_invalid")
            self.manager.delete(submitted["id"])
            self.assertFalse((self.config.data_root / "voice-results" / submitted["id"]).exists())

    def test_yingmusic_output_options_validation_and_default_compatibility(self) -> None:
        body = {"engine": "yingmusic", "source_asset_id": "a" * 32, "reference_asset_id": "b" * 32}
        with patch("server.voice.voice_capability", return_value={"available": True}):
            for options in (None, [], {"echo": 1}, {"unknown": True}, {"include_stems": "yes"}):
                with self.subTest(options=options), self.assertRaises(ApiError):
                    self.manager.submit({**body, "output_options": options})
            completed = self.wait_terminal(self.manager.submit(body)["id"])
            self.assertEqual(completed["output_options"], {"include_stems": False, "echo": True, "reverb": True})
            self.assertEqual(set(completed["outputs"]), {"mix"})
            with self.assertRaises(ApiError) as missing:
                self.manager.output_path(completed["id"], "dry_vocal")
            self.assertEqual(missing.exception.code, "voice_track_missing")
        with self.assertRaises(ApiError):
            self.manager.submit({"engine": "vevo2", "source_asset_id": "a" * 32,
                                 "reference_asset_id": "b" * 32, "output_options": {}})

    def test_active_cancel_terminates_task_and_cleans_partial_output(self) -> None:
        self.worker.block = True
        submitted = self.manager.submit({"engine": "vevo2", "source_asset_id": "a" * 32, "reference_asset_id": "b" * 32})
        self.assertTrue(self.worker.started.wait(1))
        self.manager.cancel(submitted["id"])
        terminal = self.wait_terminal(submitted["id"])
        self.assertEqual(terminal["status"], "canceled")
        self.assertFalse((self.config.data_root / "voice-results" / submitted["id"] / "converted.wav").exists())

    def test_restart_marks_inflight_task_retryable_without_resubmission(self) -> None:
        task_id = "e" * 32
        self.manager.store.put(task_id, {"id": task_id, "status": "running", "engine": "vevo2", "created_at": 1})
        temporary = self.config.data_root / "voice-results" / task_id / "yingmusic-stems"
        temporary.mkdir(parents=True)
        (temporary / "partial.wav").write_bytes(b"partial")
        recovered = VoiceTaskManager(self.config, self.assets, self.resources, self.worker)  # type: ignore[arg-type]
        value = recovered.get(task_id)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error"]["code"], "voice_task_interrupted")
        self.assertTrue(value["error"]["retryable"])
        self.assertFalse(temporary.exists())

    def test_restart_ignores_malformed_task_id_without_broad_cleanup(self) -> None:
        sentinel = self.config.data_root / "voice-results" / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        self.manager.store.put("f" * 32, {"id": "", "status": "running"})
        VoiceTaskManager(self.config, self.assets, self.resources, self.worker)  # type: ignore[arg-type]
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_unavailable_error_redacts_runtime_paths(self) -> None:
        self.manager.config = replace(self.config, vevo2_root="/private/runtime", vevo2_python="/private/python")
        with self.assertRaises(ApiError) as raised:
            self.manager.submit({
                "engine": "vevo2", "source_asset_id": "a" * 32,
                "reference_asset_id": "b" * 32,
            })
        self.assertNotIn("root", raised.exception.details)
        self.assertNotIn("python", raised.exception.details)

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups require POSIX")
    def test_worker_termination_kills_descendant_process_group(self) -> None:
        script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(child.pid,flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        assert process.stdout is not None
        child_pid = int(process.stdout.readline().strip())
        try:
            ProcessVoiceWorker._terminate(process)
            self.assertIsNotNone(process.poll())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("worker descendant survived process-group termination")
        finally:
            process.stdout.close()
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_capability_is_honest_when_runtime_is_missing(self) -> None:
        unavailable = voice_capability(replace(self.config, vevo2_root=""), "vevo2")
        self.assertFalse(unavailable["available"])
        self.assertIn("repository", unavailable["missing"])

    def test_save_asset_is_per_track_content_and_rolls_back_metadata_failure(self) -> None:
        from unittest.mock import Mock
        body = {"engine": "yingmusic", "source_asset_id": "a"*32, "reference_asset_id": "b"*32,
                "output_options": {"include_stems": True}}
        with patch("server.voice.voice_capability", return_value={"available": True}):
            task = self.wait_terminal(self.manager.submit(body)["id"])
        imported = {}
        def import_file(path, **kwargs):
            asset = {"id": str(len(imported)+1)*32, "kind": "audio"}
            imported[asset["id"]] = asset
            path.unlink()
            return asset
        self.assets.import_file = Mock(side_effect=import_file)
        self.assets.used_bytes = Mock(return_value=0)
        self.assets.public_metadata = lambda value: value
        self.assets.delete = Mock()
        old_get = self.assets.get
        self.assets.get = lambda key: imported[key] if key in imported else old_get(key)
        first = self.manager.save_asset(task['id'], {'track':'mix'})
        self.assertTrue(self.manager.output_path(task['id']).is_file())
        second = self.manager.save_asset(task['id'], {'track':'mix'})
        self.assertEqual(first['asset'], second['asset']);self.assertTrue(second['reused'])
        self.assertEqual(self.assets.import_file.call_count,1)
        with patch.object(self.manager.store,'put',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):self.manager.save_asset(task['id'], {'track':'dry_vocal'})
        self.assets.delete.assert_called_once()
        with self.assertRaises(ApiError):self.manager.save_asset(task['id'], {'track':'../../outside'})
        self.assertFalse(list(self.manager.output_root.rglob('asset-import-*')))

    def test_yingmusic_requires_every_runtime_file(self) -> None:
        repo = self.config.data_root / "yingmusic"
        (repo / "accom_separation").mkdir(parents=True)
        for name in ("my_inference.py", "accom_separation/inference.py", "model.bin"):
            (repo / name).write_text("test")
        fields = ("separator_config", "separator_checkpoint", "svc_config", "svc_checkpoint")
        config = replace(self.config, yingmusic_root=str(repo),
                         yingmusic_python=self.config.vevo2_python,
                         **{f"yingmusic_{name}": str(repo / "model.bin") for name in fields})
        with patch("server.voice.shutil.which", return_value="/usr/bin/sox"):
            self.assertTrue(voice_capability(config, "yingmusic")["available"])
            for name in fields:
                for value in ("", str(repo), str(repo / "missing")):
                    with self.subTest(field=name, value=value):
                        result = voice_capability(replace(config, **{f"yingmusic_{name}": value}), "yingmusic")
                        self.assertFalse(result["available"])
                        if not value:
                            self.assertIn(name, result["missing"])

    def test_yingmusic_worker_rejects_empty_or_directory_file_before_loading(self) -> None:
        from argparse import Namespace
        from server.voice_worker import _engine
        root = self.config.data_root
        file = root / "model.bin"
        file.write_text("test")
        fields = ("separator_config", "separator_checkpoint", "svc_config", "svc_checkpoint")
        for name in fields:
            for value in ("", str(root), str(root / "missing")):
                args = Namespace(engine="yingmusic", repo=str(root), cache_root=str(root / "cache"),
                                 device=0, **{field: str(file) for field in fields})
                setattr(args, name, value)
                with self.subTest(field=name, value=value), patch("server.voice_worker.YingMusicEngine") as engine:
                    with self.assertRaisesRegex(ValueError, name):
                        _engine(args)
                    engine.assert_not_called()

    def test_worker_status_does_not_wait_for_model_load_or_conversion(self) -> None:
        worker = ProcessVoiceWorker(self.config)
        locked = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        def hold_worker_lock() -> None:
            with worker._lock:
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_worker_lock)
        reader = threading.Thread(target=lambda: (worker.status(), returned.set()))
        holder.start()
        try:
            self.assertTrue(locked.wait(1))
            reader.start()
            self.assertTrue(returned.wait(0.2), "capability reads must not wait for GPU inference")
        finally:
            release.set()
            holder.join(2)
            if reader.ident is not None:
                reader.join(2)

    def test_public_capability_redacts_machine_paths(self) -> None:
        capability = self.manager.capabilities()["engines"][0]
        self.assertNotIn("root", capability)
        self.assertNotIn("python", capability)
        self.assertEqual(capability["repository_revision"], self.config.vevo2_revision)
        self.assertEqual(capability["model_revision"], self.config.vevo2_model_revision)

    def test_terminal_task_delete_removes_receipt_and_output(self) -> None:
        submitted = self.manager.submit({
            "engine": "vevo2", "source_asset_id": "a" * 32,
            "reference_asset_id": "b" * 32,
        })
        self.wait_terminal(submitted["id"])
        output = self.manager.output_path(submitted["id"])
        self.assertTrue(output.is_file())
        deleted = self.manager.delete(submitted["id"])
        self.assertTrue(deleted["deleted"])
        self.assertFalse(output.exists())
        with self.assertRaises(ApiError):
            self.manager.get(submitted["id"])

    def test_oversize_worker_output_fails_and_is_removed(self) -> None:
        self.manager.config = replace(self.config, max_audio_bytes=4)
        submitted = self.manager.submit({
            "engine": "vevo2", "source_asset_id": "a" * 32,
            "reference_asset_id": "b" * 32,
        })
        terminal = self.wait_terminal(submitted["id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error"]["code"], "voice_output_too_large")
        self.assertFalse((self.config.data_root / "voice-results" / submitted["id"] / "converted.wav").exists())

    def test_worker_prepends_upstream_module_root_once(self) -> None:
        module_root = Path(self.temp.name) / "upstream-modules"
        resolved = str(module_root.resolve())
        original = list(sys.path)
        try:
            sys.path.extend((resolved, resolved))
            _prepend_sys_path(module_root)
            self.assertEqual(sys.path[0], resolved)
            self.assertEqual(sys.path.count(resolved), 1)
        finally:
            sys.path[:] = original


if __name__ == "__main__":
    unittest.main()
