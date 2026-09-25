import unittest
from server.soulx_alignment import align_tracks


def note(text, duration=0.5, pitch=60, kind=2):
    return {'text': text, 'phoneme': text, 'duration': duration, 'note_pitch': pitch,
            'note_type': 1 if text == '<SP>' else kind, 'index': 0}


def units(text):
    return [{'text': c, 'phoneme': c} for c in text]


class SoulXAlignmentTests(unittest.TestCase):
    def test_more_words_than_notes_never_overwrites_words(self):
        original = [{'meta': {'time': [1500, 3500]}, 'f0': '60 61 62',
                     'tokens': [note('<SP>', .2, 0), note('甲', .8), note('乙', 1, 62)]}]
        result = align_tracks(original, [units('春风吹过山城')])[0]
        self.assertEqual(''.join(t['text'] for t in result['tokens'] if t['note_type'] == 2), '春风吹过山城')
        self.assertAlmostEqual(sum(t['duration'] for t in result['tokens']), 2)
        self.assertEqual(result['tokens'][0], original[0]['tokens'][0])
        self.assertEqual(result['f0'], original[0]['f0'])
        self.assertEqual(result['meta'], original[0]['meta'])
        self.assertEqual(original[0]['tokens'][1]['text'], '甲')
        for pitch, expected in ((60, .8), (62, 1)):
            self.assertAlmostEqual(sum(t['duration'] for t in result['tokens'] if t['note_pitch'] == pitch), expected)

    def test_fewer_words_preserve_all_pitch_intervals(self):
        source = [{'meta': {}, 'f0': 'unchanged', 'tokens': [note('甲', .4, 60), note('乙', .6, 62), note('丙', 1, 64)]}]
        output = align_tracks(source, [units('风')])[0]['tokens']
        self.assertEqual([t['note_pitch'] for t in output], [60, 62, 64])
        self.assertEqual([t['note_type'] for t in output], [2, 3, 3])
        self.assertEqual([t['duration'] for t in output], [.4, .6, 1])

    def test_lines_map_across_segments_and_keep_repeated_lyrics(self):
        source = [{'meta': {'time': [100, 1000]}, 'f0': '', 'tokens': [note('甲'), note('<SP>', .1, 0)]},
                  {'meta': {'time': [3000, 4000]}, 'f0': '', 'tokens': [note('乙')]}]
        output = align_tracks(source, [units('星星'), units('月')])
        self.assertEqual([t['text'] for t in output[0]['tokens'] if t['note_type'] == 2], ['星', '星'])
        self.assertEqual(output[1]['tokens'][0]['text'], '月')
        self.assertEqual(output[1]['meta']['time'], [3000, 4000])

    def test_capacity_split_covers_all_units(self):
        source = [{'meta': {}, 'f0': '', 'tokens': [note('甲'), note('<SP>', .1, 0), note('乙')]}]
        output = align_tracks(source, [units('春风吹过山城')])[0]['tokens']
        self.assertEqual(''.join(t['text'] for t in output if t['note_type'] == 2), '春风吹过山城')

    def test_rejects_empty_singing_or_lyrics(self):
        with self.assertRaises(ValueError):
            align_tracks([{'tokens': [note('<SP>')]}], [units('风')])
        with self.assertRaises(ValueError):
            align_tracks([{'tokens': [note('甲')]}], [])
