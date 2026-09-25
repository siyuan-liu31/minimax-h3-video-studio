"""Stage-isolated worker: separate, align confirmed words, sing each phrase, mix.

Children share the parent's process group and GPU lease for reliable cancellation.
The original instrumental is never synthesized or time-stretched.
"""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class YingSingerEngine:
    def __init__(self, manifest, device):
        self.runtime = json.loads(Path(manifest).read_text())
        self.device = device

    def run(self, request):
        output = Path(request['output'])
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='lyrics-', dir=output.parent) as tmp:
            job = {**request, 'work':tmp, 'runtime':self.runtime}
            path = Path(tmp)/'job.json'
            path.write_text(json.dumps(job, ensure_ascii=False))
            with contextlib.redirect_stdout(sys.stderr):
                self._stage('separate', path)
                if request.get('operation') == 'transcribe':
                    self._stage('transcribe', path)
                else:
                    self._stage('align', path)
                    if request.get('reference') != request['source']:
                        self._stage('reference', path)
                    self._stage('sing', path)
        return output

    def _stage(self, stage, path):
        r = self.runtime
        align = stage in {'align', 'transcribe', 'reference'}
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(self.device), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        env['PYTHONPATH'] = r['align_deps'] if stage == 'align' else (r['deps'] if not align else '')
        try:
            subprocess.run([r['align_python'] if align else r['python'], str(Path(__file__).with_name('lyrics_stage.py')), stage, str(path)],
                           env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, timeout=900, check=True)
        except subprocess.CalledProcessError as error:
            detail=path.parent/'stage-error.json'
            message=json.loads(detail.read_text()).get('message') if detail.is_file() else None
            raise RuntimeError(message or f'改词处理阶段 {stage} 失败，请查看服务端日志') from error
