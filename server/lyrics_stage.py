"""Private model stage entrypoint; only server-owned job files are accepted."""
import json
import os
from pathlib import Path
import sys


def separate(job):
    import shutil
    r = job['runtime']; w = Path(job['work']); repo = Path(r['separator_root'])
    os.chdir(repo); sys.path[:0] = [str(repo/'accom_separation'),str(repo)]
    from voice_worker import _ensure_torchaudio_wav_io
    _ensure_torchaudio_wav_io()
    import torch
    from accom_separation.utils.settings import get_model_from_config, parse_args_inference
    from accom_separation.utils.model_utils import load_start_checkpoint
    from accom_separation.inference import run_folder
    inputs=w/'inputs'; inputs.mkdir()
    # Decode to a stable filename so separator output paths cannot depend on uploads.
    import subprocess
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',job['source'],'-c:a','pcm_f32le',str(inputs/'source.wav')],check=True,timeout=300)
    args=parse_args_inference({'model_type':'bs_roformer','config_path':r['separator_config'],'start_check_point':r['separator_checkpoint'],'input_folder':str(inputs),'store_dir':str(w/'stems'),'extract_instrumental':True,'extract_other':False,'device_ids':[0],'disable_detailed_pbar':True,'force_cpu':False,'flac_file':False,'use_tta':False})
    m,c=get_model_from_config(args.model_type,args.config_path)
    load_start_checkpoint(args,m,torch.load(args.start_check_point,weights_only=False,map_location='cpu'),type_='inference')
    run_folder(m.eval().to('cuda:0'),args,c,torch.device('cuda:0'),verbose=False)
    for name in ('vocals.wav','instrumental.wav'):
        shutil.copy2(w/'stems/source'/name,w/name)


def align(job):
    import torch
    import soundfile as sf
    from qwen_asr import Qwen3ForcedAligner
    from lyrics_timing import lyric_lines, line_bounds
    r=job['runtime']; w=Path(job['work'])
    old,new=lyric_lines(job['original_lyrics']),lyric_lines(job['lyrics'])
    if len(old)!=len(new): raise ValueError('原词、新词句数不一致')
    model=Qwen3ForcedAligner.from_pretrained(r['align_models'],dtype=torch.float32,device_map='cpu')
    rows=model.align(audio=str(w/'vocals.wav'),text=''.join(old),language='Chinese')[0]
    words=[{'text':x.text,'start':x.start_time,'end':x.end_time} for x in rows]
    bounds=line_bounds(old,words,sf.info(w/'vocals.wav').duration)
    (Path(job['output']).parent/'alignment.json').write_text(json.dumps({'original_lines':old,'target_lines':new,'words':words,'bounds':bounds},ensure_ascii=False))


def transcribe(job):
    from funasr import AutoModel
    w=Path(job['work']); r=job['runtime']
    model=AutoModel(model=r['asr_models'],device='cpu',disable_update=True)
    rows=model.generate(input=str(w/'vocals.wav'))
    text=''.join(row.get('text','').replace(' ','') for row in rows)
    stamps=[s for row in rows for s in row.get('timestamp',[])]
    # Speech punctuation is unreliable for singing. Use acoustic gaps for lines.
    if len(stamps)==len(text):
        lines=[]; line=''
        for i,ch in enumerate(text):
            line+=ch
            if i+1==len(text) or stamps[i+1][0]-stamps[i][1]>.35*1000 or len(line)>=18:
                lines.append(line);line=''
        from lyrics_timing import merge_short_transcription_lines
        text='\n'.join(merge_short_transcription_lines(lines))
    Path(job['output']).write_text(json.dumps({'lyrics':text},ensure_ascii=False))


def sing(job):
    import numpy as np
    import soundfile as sf
    import torch
    from voice_worker import _ensure_torchaudio_wav_io
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from server.voice_mix import remix_audio
    _ensure_torchaudio_wav_io()
    r=job['runtime']; w=Path(job['work']); out=Path(job['output']); os.chdir(r['root']);sys.path.insert(0,r['root'])
    from src.YingMusicSinger.infer.YingMusicSinger import YingMusicSinger
    data=json.loads((out.parent/'alignment.json').read_text()); bounds=data['bounds']
    voice,sr=sf.read(w/'vocals.wav',dtype='float32',always_2d=True)
    backing,back_sr=sf.read(w/'instrumental.wav',dtype='float32',always_2d=True)
    if sr!=back_sr or voice.shape!=backing.shape:raise ValueError('分离轨时间轴不一致')
    m=YingMusicSinger.from_pretrained(r['models'],local_files_only=True).eval().to('cuda:0')
    a,b=bounds[0];ref=w/'reference.wav';sf.write(ref,voice[round(a*sr):round(b*sr)],sr,subtype='FLOAT')
    duration=bounds[0][1] if job.get('preview') else len(voice)/sr
    count=round(duration*sr); full=np.zeros_like(voice[:count]);params=job['parameters']
    for i,(a,b) in enumerate(bounds[:1] if job.get('preview') else bounds):
        chunk=w/'phrase.wav';sf.write(chunk,voice[round(a*sr):round(b*sr)],sr,subtype='FLOAT')
        with torch.inference_mode():
            y,rate=m(ref_audio_path=str(ref),melody_audio_path=str(chunk),ref_text=data['original_lines'][0],target_text=data['target_lines'][i],nfe_step=params['diffusion_steps'],cfg_strength=params['inference_cfg_rate'],seed=params['seed'])
        wave=y.cpu().numpy().T
        if rate!=sr or not np.isfinite(wave).all() or abs(len(wave)/sr-(b-a))>.15:raise ValueError('生成乐句时长或音频无效')
        n=min(len(wave),round(b*sr)-round(a*sr),len(full)-round(a*sr))
        # Short sample-grid differences are padded, never stretch the melody.
        full[round(a*sr):round(a*sr)+n]=wave[:n]
    original_rms=float(np.sqrt(np.mean(voice[:count].astype('float64')**2))); rms=float(np.sqrt(np.mean(full.astype('float64')**2)))
    if rms<1e-5:raise ValueError('未生成有效演唱')
    full*=np.clip(original_rms/rms,.25,4)
    sf.write(out.parent/'dry-vocal.wav',full,sr,subtype='FLOAT')
    sf.write(out.parent/'accompaniment.wav',backing[:count],sr,subtype='FLOAT')
    import hashlib
    digest=lambda array: hashlib.sha256(array.astype('<f4').tobytes()).hexdigest()
    retained,_=sf.read(out.parent/'accompaniment.wav',dtype='float32',always_2d=True)
    report={'model_revision':r['model_revision'],'repository_revision':r['repository_revision'],
            'aligner_revision':r['aligner_revision'],'sample_rate':sr,'samples':count,
            'separated_backing_sha256':digest(backing[:count]),'retained_backing_sha256':digest(retained),
            'phrases':1 if job.get('preview') else len(bounds)}
    if report['separated_backing_sha256']!=report['retained_backing_sha256']:raise ValueError('伴奏一致性校验失败')
    (out.parent/'render-report.json').write_text(json.dumps(report))
    remix_audio(out.parent/'dry-vocal.wav',out.parent/'accompaniment.wav',out,{'vocal_gain_db':0,'accompaniment_gain_db':0})


if __name__=='__main__':
    stage,path=sys.argv[1:]
    job=json.loads(Path(path).read_text())
    try:
        {'separate':separate,'align':align,'transcribe':transcribe,'sing':sing}[stage](job)
    except Exception as error:
        (Path(job['work'])/'stage-error.json').write_text(json.dumps({'message':str(error)},ensure_ascii=False))
        raise
