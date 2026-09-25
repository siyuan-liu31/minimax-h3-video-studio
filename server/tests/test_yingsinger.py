import unittest
from unittest.mock import patch
from server.lyrics_timing import line_bounds, lyric_lines
from server.yingsinger import rewrite_parameters, capability
from server.errors import ApiError
from server.tests import test_voice
from types import SimpleNamespace


class TimingTests(unittest.TestCase):
    def test_whole_words_and_midpoint_padding(self):
        rows=[{'text':'春','start':.2,'end':1.0},{'text':'风','start':1.12,'end':2.0}]
        bounds=line_bounds(['春','风'],rows,3)
        self.assertEqual(bounds[0][1],bounds[1][0])
        self.assertEqual(bounds[0][0],.1)
        self.assertEqual(bounds[1][1],2.1)
    def test_rejects_dropped_duplicated_and_zero_duration_words(self):
        for rows in [[],[{'text':'风','start':.2,'end':1}], [{'text':'春','start':1,'end':1}], [{'text':'春','start':float('nan'),'end':1}]]:
            with self.subTest(rows=rows),self.assertRaises(ValueError):line_bounds(['春'],rows,2)
    def test_rejects_overlaps_and_long_phrases(self):
        with self.assertRaises(ValueError):line_bounds(['春风'],[{'text':'春','start':0,'end':1},{'text':'风','start':.5,'end':2}],3)
        with self.assertRaises(ValueError):line_bounds(['春'],[{'text':'春','start':0,'end':25}],30)
    def test_confirmed_lines_required_without_silent_character_removal(self):
        self.assertEqual(lyric_lines('春风，\n\n明月。'),['春风','明月'])
        for value in ['春风2','[Verse]春风','hello','']:
            with self.assertRaises(ValueError):lyric_lines(value)
        with self.assertRaises(ApiError):rewrite_parameters({'lyrics':'春风\n明月','original_lyrics':'风'})
    def test_transcript_breath_does_not_split_short_phrase(self):
        from server.lyrics_timing import merge_short_transcription_lines
        self.assertEqual(merge_short_transcription_lines(['月照入','心头','独醉相思愁','几时休']),['月照入心头','独醉相思愁','几时休'])
    def test_unconfigured_engine_is_unavailable(self):
        self.assertFalse(capability(SimpleNamespace(lyrics_runtime=''))['available'])


class TaskTests(unittest.TestCase):
    setUp=test_voice.VoiceTaskTests.setUp
    tearDown=test_voice.VoiceTaskTests.tearDown
    wait_terminal=test_voice.VoiceTaskTests.wait_terminal
    def test_phrase_preview_tracks_survive_reload_and_replay(self):
        self.assets.values['a'*32]['media']={'duration':19}
        body={'engine':'yingsinger','source_asset_id':'a'*32,'lyrics':'新歌词','original_lyrics':'原歌词','preview':True,'request_id':'e'*32}
        with patch('server.voice.voice_capability',return_value={'available':True}),patch('server.voice.voice_model_key',return_value='yingsinger:test'):
            task=self.manager.submit(body);task=self.wait_terminal(task['id'])
            self.assertEqual(task['status'],'completed')
            self.assertTrue(task['preview'])
            self.assertEqual(set(task['outputs']),{'mix','dry_vocal','accompaniment'})
            self.assertEqual(self.manager.submit(body)['id'],task['id'])
            self.assertEqual(self.worker.requests[0]['original_lyrics'],'原歌词')
            self.assertEqual(self.manager.get(task['id'])['engine'],'yingsinger')
    def test_rejects_other_timbre_and_unknown_duration(self):
        body={'engine':'yingsinger','source_asset_id':'a'*32,'lyrics':'新歌词','original_lyrics':'原歌词'}
        with self.assertRaises(ApiError):self.manager.submit(body)
        self.assets.values['a'*32]['media']={'duration':19}
        with self.assertRaises(ApiError):self.manager.submit({**body,'reference_asset_id':'b'*32})
