import array
import math
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from server.voice_mix import mix_parameters, remix_audio
from server.errors import ApiError

class MixTests(unittest.TestCase):
    def test_rejects_nonfinite_and_unknown_parameters(self):
        for value in (True, '3', float('nan'), float('inf'), -19, 13):
            with self.subTest(value=value), self.assertRaises(ApiError):
                mix_parameters({'vocal_gain_db': value})
        with self.assertRaises(ApiError): mix_parameters({'command': 'anything'})

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_gain_balance_peak_protection_preserves_original_stems(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            # Distinct tones let correlation measure the actual relative gain.
            for name, frequency in [('vocal', 400), ('backing', 1000)]:
                samples=array.array('h', [round(24000*math.sin(2*math.pi*frequency*i/24000)) for i in range(24000)])
                with wave.open(str(root/f'{name}.wav'),'wb') as out:
                    out.setparams((1,2,24000,0,'NONE',''));out.writeframes(samples.tobytes())
            original=(root/'vocal.wav').read_bytes()
            remix_audio(root/'vocal.wav',root/'backing.wav',root/'mix.wav',{'vocal_gain_db':-3,'accompaniment_gain_db':3})
            with wave.open(str(root/'mix.wav')) as mixed:
                self.assertEqual(mixed.getnframes(),24000)
                samples=array.array('h',mixed.readframes(24000))
            self.assertLess(max(abs(v) for v in samples),32767)
            amplitude=lambda f: abs(sum(v*math.sin(2*math.pi*f*i/24000) for i,v in enumerate(samples)))
            self.assertAlmostEqual(amplitude(1000)/amplitude(400),10**(6/20),places=2)
            self.assertEqual((root/'vocal.wav').read_bytes(),original)
