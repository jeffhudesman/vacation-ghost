import io
from contextlib import redirect_stdout
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from tools import generate_sounds
from vacation_ghost.outputs import SOUND_CUES


class AudioCueTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "sounds"

    def generate(self) -> None:
        argv = ["generate_sounds.py", str(self.directory)]
        with patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            generate_sounds.main()

    def test_creates_distinct_quiet_pcm_cues(self) -> None:
        self.generate()
        paths = list(self.directory.glob("*.wav"))
        self.assertCountEqual([path.stem for path in paths], SOUND_CUES)
        audio_data = set()
        for path in paths:
            with self.subTest(cue=path.stem), wave.open(str(path), "rb") as cue:
                self.assertEqual(
                    (cue.getnchannels(), cue.getsampwidth(), cue.getframerate()), (1, 2, 16_000)
                )
                self.assertEqual(cue.getcomptype(), "NONE")
                self.assertGreaterEqual(cue.getnframes(), 16_000)
                self.assertLessEqual(cue.getnframes(), 12 * 16_000)
                data = cue.readframes(cue.getnframes())
                audio_data.add(data)
                samples = tuple(sample for (sample,) in struct.iter_unpack("<h", data))
                self.assertEqual((samples[0], samples[-1]), (0, 0))
                peak = max(map(abs, samples))
                self.assertGreater(peak, 0)
                self.assertLessEqual(peak, round(32_767 * 0.32))
        self.assertEqual(len(audio_data), len(SOUND_CUES))

    def test_repeat_preserves_custom_cue_and_reproduces_missing_cue(self) -> None:
        self.generate()
        custom = self.directory / "tv.wav"
        custom.write_bytes(b"custom cue")
        missing = self.directory / "kettle.wav"
        original = missing.read_bytes()
        missing.unlink()
        self.generate()
        self.assertEqual(custom.read_bytes(), b"custom cue")
        self.assertEqual(missing.read_bytes(), original)
