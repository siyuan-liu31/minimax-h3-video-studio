"""Strict lyric-line boundaries; no dropped words or cross-line redistribution."""
import math
import re


def lyric_lines(text):
    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        # Initial validated pipeline is Mandarin. Reject unsupported phonetics,
        # rather than silently removing letters or guessing number readings.
        line = re.sub(r'[\s，。！？、；：,.!?;:「」“”‘’（）()—…·]', '', raw)
        if not line or any(not '\u4e00' <= c <= '\u9fff' for c in line):
            raise ValueError('目前分句改词支持中文汉字；请将数字写成汉字，去掉英文和段落标签')
        lines.append(line)
    if not lines:
        raise ValueError('请填写完整原词，并将新词按相同的句数分行')
    return lines


def line_bounds(lines, words, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('音频时长无效')
    text = ''.join(lines)
    if len(words) != len(text) or ''.join(w.get('text', '') for w in words) != text:
        raise ValueError('原词对齐不完整，请校正原词后重试')
    previous = 0.0
    for word in words:
        a, b = word['start'], word['end']
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (a, b)) or a < previous or b <= a or b > duration + .05:
            raise ValueError('有原词无法定位到演唱时间，已停止生成；请检查漏字、多字或错误分行')
        previous = b
    bounds, offset = [], 0
    for line in lines:
        group = words[offset:offset+len(line)]
        previous_end = words[offset-1]['end'] if offset else 0
        next_start = words[offset+len(line)]['start'] if offset+len(line) < len(words) else duration
        a = max(0, group[0]['start']-.1, (previous_end+group[0]['start'])/2)
        b = min(duration, group[-1]['end']+.1, (next_start+group[-1]['end'])/2)
        if b-a > 20 or b-a < .4:
            raise ValueError('每句须对应 0.4–20 秒演唱；请按原曲乐句重新分行')
        bounds.append([a, b]); offset += len(line)
    return bounds


def merge_short_transcription_lines(lines):
    """Join a split short phrase, but retain an independent short ending line."""
    result=[]; i=0
    while i<len(lines):
        if i+1<len(lines) and len(lines[i])<=3 and len(lines[i+1])<=3:
            result.append(lines[i]+lines[i+1]);i+=2
        else:
            result.append(lines[i]);i+=1
    return result


def validate_vocal_coverage(words, energy, frame_seconds=.02):
    """Reject long, audible stretches left without any confirmed source words.

    This is a missing-phrase guard, not an ASR accuracy score. Short breaths and
    quiet reverb tails are allowed; it never fills missing words by guessing.
    """
    if not energy or not words:
        raise ValueError('未检测到可用原唱')
    ordered=sorted(energy)
    threshold=max(1e-4, ordered[int((len(ordered)-1)*.75)]*.2)
    gaps=[(0,words[0]['start'])]
    gaps.extend((a['end'],b['start']) for a,b in zip(words,words[1:]))
    gaps.append((words[-1]['end'],len(energy)*frame_seconds))
    for start,end in gaps:
        if end-start<.65:
            continue
        left=max(0,round((start+.12)/frame_seconds))
        right=min(len(energy),round((end-.12)/frame_seconds))
        audible=sum(v>threshold for v in energy[left:right])*frame_seconds
        if audible>=.3:
            raise ValueError(f'原词可能漏字：{start:.1f}–{end:.1f} 秒仍有人声却没有对应文字，请补齐原词后重试')
