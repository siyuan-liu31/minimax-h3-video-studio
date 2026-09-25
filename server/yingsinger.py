"""Versioned contract for Mandarin, phrase-locked lyric replacement."""
import json
from pathlib import Path
from .errors import ApiError
from .soulx import rewrite_parameters as tuning_parameters
from .lyrics_timing import lyric_lines

REPOSITORY_REVISION = 'baa409c2e7e5e775f09b4e92a220808f4827d2cc'
MODEL_REVISION = '9b3f444f2bccd77fbc03e32eb7c334fc96040b8d'
ALIGNER_REVISION = 'c7cbfc2048c462b0d63a45797104fc9db3ad62b7'


def rewrite_parameters(data):
    lyrics, requested, effective = tuning_parameters({"diffusion_steps":64, **data})
    try:
        original, target = lyric_lines(lyrics['original_lyrics']), lyric_lines(lyrics['lyrics'])
        if len(original) != len(target):
            raise ValueError('原词与新词必须逐行对应、句数相同；一行对应一个演唱乐句')
    except ValueError as error:
        raise ApiError(400, 'invalid_lyrics', str(error)) from error
    return lyrics, requested, effective


def capability(config):
    manifest = getattr(config, 'lyrics_runtime', '')
    missing, runtime = [], {}
    try:
        runtime = json.loads(Path(manifest).read_text()) if manifest else {}
        if not isinstance(runtime, dict):
            raise ValueError("invalid runtime manifest")
        for key in ('python', 'root', 'models', 'deps', 'separator_root', 'separator_config', 'separator_checkpoint', 'align_python', 'align_deps', 'align_models', 'asr_models'):
            if not runtime.get(key) or not Path(runtime[key]).exists():
                missing.append(key)
        for root, file in (('models','model.safetensors'), ('models','config.json'), ('root','src/YingMusicSinger/infer/YingMusicSinger.py'), ('align_models','model.safetensors'), ('asr_models','model.pt')):
            if not (Path(runtime.get(root, '/nonexistent')) / file).is_file():
                missing.append(root + '/' + file)
        if runtime.get('model_revision') != MODEL_REVISION or runtime.get('repository_revision') != REPOSITORY_REVISION or runtime.get('aligner_revision') != ALIGNER_REVISION:
            missing.append('pinned revisions')
    except (OSError, ValueError, TypeError):
        missing.append('runtime manifest')
    return {'id':'yingsinger', 'available':not missing, 'mode':'phrase_lyrics_rewrite',
            'root':runtime.get('root',''), 'python':runtime.get('python',''),
            'model_revision':MODEL_REVISION, 'repository_revision':REPOSITORY_REVISION,
            'missing':missing, 'reason':'; '.join(missing) if missing else None}
