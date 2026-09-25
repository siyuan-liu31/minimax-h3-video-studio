from __future__ import annotations
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from server.errors import ApiError
from server.soulx import REQUIRED_MODELS, capability, rewrite_parameters
from server.tests import test_voice


class RewriteContractTests(unittest.TestCase):
    def test_capability_requires_both_pitch_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("core/soulsx_singer.py", "SoulX-Singer/cli/inference.py", "python"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            config = SimpleNamespace(soulx_root=str(root), soulx_python=str(root / "python"), soulx_models=str(root / "models"), soulx_revision="test")
            for name in REQUIRED_MODELS:
                path = root / "models/Soul-AILab" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertTrue(capability(config)["available"])
            pitch = "SoulX-Singer-Preprocess/rosvot/rmvpe/model.pt"
            (root / "models/Soul-AILab" / pitch).unlink()
            self.assertFalse(capability(config)["available"])
            self.assertIn(pitch, capability(config)["missing"])

    def test_rejects_invalid_text_and_tuning(self):
        for fields in ({'lyrics': ''}, {'lyrics': []}, {'lyrics': 'a'*10001}, {'original_lyrics': 42},
                       {'diffusion_steps': 15}, {'diffusion_steps': True}, {'inference_cfg_rate': float('nan')},
                       {'inference_cfg_rate': 11}, {'seed': 2**32}, {'seed': 1.5}):
            with self.subTest(fields=str(fields)[:50]), self.assertRaises(ApiError):
                rewrite_parameters({'lyrics': '新的歌词', **fields})


class RewriteTaskTests(unittest.TestCase):
    setUp = test_voice.VoiceTaskTests.setUp
    tearDown = test_voice.VoiceTaskTests.tearDown
    wait_terminal = test_voice.VoiceTaskTests.wait_terminal
    def test_rewrite_preserves_text_seed_tracks_and_idempotency(self):
        body = {'engine': 'soulx', 'source_asset_id': 'a'*32, 'reference_asset_id': 'b'*32,
                'lyrics': '月光洒在窗前\n晚风吹过山间', 'original_lyrics': '原词', 'request_id': 'e'*32}
        with patch('server.voice.voice_capability', return_value={'available': True}):
            first = self.manager.submit(body)
            completed = self.wait_terminal(first['id'])
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['lyrics'], body['lyrics'])
            self.assertEqual(completed['parameters']['inference_cfg_rate'], 3)
            self.assertEqual(set(completed['outputs']), {'mix', 'dry_vocal', 'accompaniment'})
            repeated = self.manager.submit(body)
            self.assertEqual(repeated['id'], first['id'])
            self.assertEqual(repeated['parameters'], completed['parameters'])
            with self.assertRaises(ApiError) as error:
                self.manager.submit({**body, 'lyrics': '另一个版本'})
            self.assertEqual(error.exception.code, 'idempotency_conflict')
        self.assertEqual(self.worker.requests[-1]['lyrics'], body['lyrics'])
        self.assertTrue(self.manager.output_path(first['id'], 'dry_vocal').exists())
        self.manager.delete(first['id'])
        self.assertFalse((self.manager.output_root/first['id']).exists())

    def test_lyrics_rejected_on_voice_conversion(self):
        with self.assertRaises(ApiError):
            self.manager.submit({'engine':'vevo2','source_asset_id':'a'*32,'reference_asset_id':'b'*32,'lyrics':'新词'})

    def test_transcription_has_text_receipt_and_preview_is_forwarded(self):
        def transcribe(engine, request, cancel):
            import json
            self.worker.requests.append(request)
            Path(request['output']).write_text(json.dumps({'lyrics':'识别到的原词'}))
            return {'ok':True}
        with patch('server.voice.voice_capability', return_value={'available': True}), patch.object(self.worker,'run',side_effect=transcribe):
            submitted=self.manager.submit({'engine':'soulx','operation':'transcribe','source_asset_id':'a'*32})
            completed=self.wait_terminal(submitted['id'])
            self.assertEqual(completed['status'],'completed')
            self.assertEqual(completed['detected_lyrics'],'识别到的原词')
            self.assertNotIn('output',completed)
            self.assertEqual(completed['reference_asset_id'],'a'*32)
        with patch('server.voice.voice_capability', return_value={'available': True}):
            preview=self.manager.submit({'engine':'soulx','preview':True,'source_asset_id':'a'*32,'lyrics':'新歌词'})
            self.assertEqual(self.wait_terminal(preview['id'])['status'],'completed')
            self.assertTrue(self.worker.requests[-1]['preview'])

    def test_remix_keeps_baseline_and_receipt_survives_reload(self):
        with patch('server.voice.voice_capability', return_value={'available': True}):
            task=self.manager.submit({'engine':'soulx','source_asset_id':'a'*32,'lyrics':'新词'})
            task=self.wait_terminal(task['id'])
        baseline=self.manager.output_path(task['id']).read_bytes()
        def mix(vocal,backing,output,parameters): output.write_bytes(b'RIFFremixed')
        with patch('server.voice.remix_audio',side_effect=mix):
            updated=self.manager.remix(task['id'],{'vocal_gain_db':-3,'accompaniment_gain_db':3})
        self.assertIn('remix',updated['outputs'])
        self.assertEqual(self.manager.output_path(task['id']).read_bytes(),baseline)
        self.assertEqual(self.manager.get(task['id'])['mix_parameters']['vocal_gain_db'],-3)
        self.assertEqual(self.manager.output_path(task['id'],'remix').read_bytes(),b'RIFFremixed')
