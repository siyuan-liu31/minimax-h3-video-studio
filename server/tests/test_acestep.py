import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from server.acestep import REQUIRED, capability, rewrite_parameters
from server.errors import ApiError
from server.tests import test_voice


class AceStepContractTests(unittest.TestCase):
    def test_defaults_and_invalid_inputs(self):
        _, requested, effective = rewrite_parameters({'lyrics': '新的歌词'})
        self.assertEqual(requested['diffusion_steps'], 50)
        self.assertEqual(requested['inference_cfg_rate'], 7)
        self.assertEqual(requested['audio_cover_strength'], 1)
        self.assertGreaterEqual(effective['seed'], 0)
        for values in ({'lyrics': '字'*4097}, {'caption':'x'*513}, {'caption':[]},
                       {'audio_cover_strength': float('nan')}, {'audio_cover_strength':True},
                       {'inference_cfg_rate':0.2}, {'inference_cfg_rate':0.5}, {'audio_cover_strength':-0.1}, {'audio_cover_strength':1.1}):
            with self.subTest(values=str(values)[:40]), self.assertRaises(ApiError):
                rewrite_parameters({'lyrics':'新词', **values})

    def test_capability_needs_all_xl_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['acestep/inference.py', 'python', *REQUIRED]:
                p = root/name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            config = SimpleNamespace(acestep_root=str(root), acestep_models=str(root), acestep_python=str(root/'python'))
            self.assertTrue(capability(config)['available'])
            self.assertEqual(capability(config)['model'], 'acestep-v15-xl-sft')
            self.assertIsNone(capability(config)['quantization'])
            self.assertEqual(capability(config)['tuning']['inference_cfg_rate']['minimum'],1)
            (root/REQUIRED[0]).unlink()
            self.assertFalse(capability(config)['available'])


class AceStepTaskTests(unittest.TestCase):
    setUp = test_voice.VoiceTaskTests.setUp
    tearDown = test_voice.VoiceTaskTests.tearDown
    wait_terminal = test_voice.VoiceTaskTests.wait_terminal

    def test_cover_queue_tracks_and_idempotency(self):
        self.assets.values['a'*32]['media'] = {'duration':19}
        body = {'engine':'acestep', 'source_asset_id':'a'*32, 'lyrics':'新歌词',
                'caption':'Chinese pop', 'audio_cover_strength':0.8, 'request_id':'e'*32}
        with patch('server.voice.voice_capability', return_value={'available':True}):
            task = self.manager.submit(body)
            done = self.wait_terminal(task['id'])
            self.assertEqual(done['status'],'completed')
            self.assertEqual(set(done['outputs']), {'mix'})
            self.assertEqual(done['reference_asset_id'], 'a'*32)
            self.assertEqual(done['parameters']['caption'],'Chinese pop')
            self.assertEqual(self.worker.requests[0]['parameters']['audio_cover_strength'],0.8)
            self.assertEqual(self.manager.submit(body)['id'], task['id'])
            with self.assertRaises(ApiError) as error:
                self.manager.submit({**body, 'audio_cover_strength':0.7})
            self.assertEqual(error.exception.code,'idempotency_conflict')
        self.assertTrue(self.manager.output_path(task['id']).is_file())
        with self.assertRaises(ApiError):
            self.manager.output_path(task['id'],'dry_vocal')

    def test_reject_unsupported_operations_and_duration(self):
        for values in ({'operation':'transcribe'}, {'preview':True}, {}):
            with self.subTest(values=values), self.assertRaises(ApiError):
                self.manager.submit({'engine':'acestep','source_asset_id':'a'*32,'lyrics':'新词', **values})
