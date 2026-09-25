"""Lossless lyric assignment: preserve rests, pitch boundaries and F0 time axis."""
from __future__ import annotations

from copy import deepcopy


def sections(tokens):
    result, current = [], []
    for token in tokens:
        if token['text'] == '<SP>':
            if current:
                result.append(current)
                current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def slots(tokens):
    result = []
    for token in tokens:
        if result and token['text'] == result[-1][-1]['text'] and token['note_type'] == 3:
            result[-1].append(token)
        else:
            result.append([token])
    return result


def align_section(tokens, units):
    """Map every lyric unit onto contiguous original syllable slots, splitting
    notes at unit boundaries. No original pitch interval or lyric is discarded.
    """
    if not units:
        return [{**t, 'text': '<SP>', 'phoneme': '<SP>', 'note_type': 1} for t in tokens]
    grouped = slots(tokens)
    count = len(units)
    result = []
    previous_unit = None
    for slot_index, group in enumerate(grouped):
        duration = sum(t['duration'] for t in group)
        if duration <= 0:
            raise ValueError('source has invalid note duration')
        elapsed = 0.0
        for token in group:
            start = slot_index + elapsed / duration
            elapsed += token['duration']
            end = slot_index + elapsed / duration
            cursor = start
            while cursor < end - 1e-9:
                index = min(count - 1, int((cursor + 1e-9) * count / len(grouped)))
                boundary = min(end, (index + 1) * len(grouped) / count)
                length = (boundary - cursor) * duration
                unit = units[index]
                result.append({**token, 'text': unit['text'], 'phoneme': unit['phoneme'],
                               'duration': length, 'note_type': 3 if index == previous_unit else 2})
                previous_unit = index
                cursor = boundary
    heads = [t['text'] for t in result if t['note_type'] == 2]
    if heads != [u['text'] for u in units]:
        raise ValueError('lyric alignment lost a syllable')
    return result


def align_tracks(tracks, lyric_lines):
    """A matching line count maps one line per vocal phrase; otherwise divide
    units by original syllable capacity. Metadata/F0/rests remain unchanged.
    """
    result = deepcopy(tracks)
    phrases = [part for track in result for part in sections(track['tokens'])]
    if not phrases:
        raise ValueError('no singing detected in source audio')
    all_units = [unit for line in lyric_lines for unit in line]
    if not all_units:
        raise ValueError('new lyrics contain no supported singing syllables')
    if len(lyric_lines) == len(phrases):
        assignments = lyric_lines
    else:
        capacities = [len(slots(part)) for part in phrases]
        total, cumulative, previous = sum(capacities), 0, 0
        assignments = []
        for capacity in capacities:
            cumulative += capacity
            end = round(len(all_units) * cumulative / total)
            assignments.append(all_units[previous:end])
            previous = end
    phrase_index = 0
    for track in result:
        output, current = [], []
        for token in track['tokens'] + [None]:
            if token is None or token['text'] == '<SP>':
                if current:
                    output.extend(align_section(current, assignments[phrase_index]))
                    phrase_index += 1
                    current = []
                if token is not None:
                    output.append(token)
            else:
                current.append(token)
        for index, token in enumerate(output):
            token['index'] = index
        track['tokens'] = output
    return result
