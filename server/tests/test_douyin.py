from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from server.douyin import DouyinTasks, extract_url, ACTIVE
from server.errors import ApiError
from server.storage import AssetStore
from server.tests.test_app import make_config


class DouyinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.tmp.name))
        self.config.prepare()
        self.assets = AssetStore(self.config)
        class Media:
            def quota_bytes(self): return 0
        self.manager = DouyinTasks(self.config, self.assets, threading.RLock(), Media())
        self.cap = patch.object(self.manager, 'capabilities', return_value={'available': True})
        self.cap.start()

    def tearDown(self):
        self.manager.stop()
        self.cap.stop()
        self.tmp.cleanup()

    def wait(self, task):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = self.manager.get(task['id'])
            if value['status'] not in ACTIVE: return value
            time.sleep(.02)
        self.fail('task did not finish')

    def test_url_boundary(self):
        self.assertEqual(extract_url('复制 https://v.douyin.com/abc/。'), 'https://v.douyin.com/abc/')
        for url in ['http://douyin.com/video/1','https://douyin.com.evil/video/1','https://root@douyin.com/video/1','https://douyin.com:443/video/1', 'https://127.0.0.1/x', '--exec x']:
            with self.subTest(url=url), self.assertRaises(ApiError): extract_url(url)

    def test_parse_filters_secrets_and_idempotency(self):
        raw = {'id':'123', 'title':'Video', 'url':'https://signed/secret','cookies':'secret'}
        with patch.object(self.manager, 'command', return_value=json.dumps(raw)):
            task = self.manager.submit({'text':'https://douyin.com/video/123','mode':'parse','request_id':'a'*32})
            result = self.wait(task)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['metadata'], {'id':'123','title':'Video'})
            self.assertEqual(self.manager.submit({'text':task['url'],'mode':'parse','request_id':'a'*32})['id'], task['id'])
            with self.assertRaises(ApiError): self.manager.submit({'text':task['url'],'request_id':'a'*32})

    def test_cancel_retry_restart_and_cleanup(self):
        def blocked(args,event,work,progress=None):
            event.wait(3)
            raise ApiError(409,'canceled','Canceled')
        with patch.object(self.manager,'command',side_effect=blocked):
            task = self.manager.submit({'text':'https://douyin.com/video/123'})
            self.manager.cancel(task['id'])
            self.assertEqual(self.wait(task)['status'],'canceled')
        with patch.object(self.manager,'command',return_value=json.dumps({'id':'123'})):
            task = self.manager.retry(task['id'])
            self.assertEqual(self.wait(task)['error']['code'],'download_missing')
        self.assertEqual(list((self.config.data_root/'tmp').glob('douyin-*')),[])
        self.manager.update(task['id'],status='running')
        restarted = DouyinTasks(self.config,self.assets,threading.RLock(),self.manager.media)
        self.assertEqual(restarted.get(task['id'])['error']['code'],'interrupted')
        restarted.stop()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'media tools required')
    def test_download_import_and_exact_duplicate(self):
        video = Path(self.tmp.name)/'fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=64x64:rate=24','-t','0.5','-c:v','libx264',str(video)],check=True)
        def command(args,event,work,progress=None):
            if '--dump-single-json' in args: return json.dumps({'id':'123','title':'fixture'})
            shutil.copyfile(video,work/'video.mp4')
            progress('progress:{"downloaded_bytes":5,"total_bytes":10}')
            return ''
        with patch.object(self.manager,'command',side_effect=command):
            first = self.wait(self.manager.submit({'text':'https://douyin.com/video/123'}))
            self.assertEqual(first['status'],'completed',first)
            self.assertEqual(first['asset']['kind'],'video')
            second = self.wait(self.manager.submit({'text':'https://douyin.com/video/123'}))
            self.assertTrue(second['reused'])
            self.assertEqual(second['asset_id'],first['asset_id'])
            self.assertEqual(len(self.assets.list()),1)

    def test_process_errors_are_redacted_and_cancellable(self):
        script=Path(self.tmp.name)/'fake'
        script.write_text('#!/bin/sh\necho "fresh cookies required secret-token" >&2\nexit 1\n')
        script.chmod(0o700)
        self.manager.executable=str(script)
        task=self.wait(self.manager.submit({'text':'https://douyin.com/video/123'}))
        self.assertEqual(task['error']['code'],'cookie_refresh_required')
        self.assertNotIn('secret-token',json.dumps(task))
        script.write_text('#!/bin/sh\nsleep 30\n')
        task=self.manager.submit({'text':'https://douyin.com/video/123'})
        time.sleep(.1)
        self.manager.cancel(task['id'])
        self.assertEqual(self.wait(task)['status'],'canceled')

    def test_process_access_errors_have_distinct_feedback_codes(self):
        script=Path(self.tmp.name)/'fake'
        self.manager.executable=str(script)
        for status, code in ((403, 'access_restricted'), (429, 'rate_limited')):
            with self.subTest(status=status):
                script.write_text(f'#!/bin/sh\necho "HTTP Error {status}: Forbidden" >&2\nexit 1\n')
                script.chmod(0o700)
                task=self.wait(self.manager.submit({'text':f'https://douyin.com/video/{status}'}))
                self.assertEqual(task['status'], 'failed')
                self.assertEqual(task['error']['code'], code)

    def test_cancel_wins_over_concurrent_extractor_failure(self):
        entered = threading.Event()
        def command(args, event, work, progress=None):
            entered.set()
            event.wait(3)
            raise ApiError(422, "cookie_refresh_required", "Session expired")
        with patch.object(self.manager, 'command', side_effect=command):
            task = self.manager.submit({'text': 'https://douyin.com/video/123'})
            self.assertTrue(entered.wait(2))
            self.manager.cancel(task['id'])
            result = self.wait(task)
            self.assertEqual(result['status'], 'canceled')
            self.assertEqual(result['error']['code'], 'canceled')

    def test_queue_limit_and_strict_request(self):
        with self.assertRaises(ApiError): self.manager.submit({'text':'https://douyin.com/video/123','cookies':'secret'})
        with patch.object(self.manager,'run',side_effect=lambda t,e:e.wait(2)):
            for _ in range(8): self.manager.submit({'text':'https://douyin.com/video/123'})
            with self.assertRaises(ApiError) as caught: self.manager.submit({'text':'https://douyin.com/video/123'})
            self.assertEqual(caught.exception.status,429)
