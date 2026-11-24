import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vacation_ghost.config import load_config
from vacation_ghost.outputs import PiOutputs, SOUND_CUES


class PiOutputsTest(unittest.TestCase):
    def setUp(self):
        self.plugs = {"kitchen": "192.168.1.20", "living": "192.168.1.21"}

    def test_lights_only_configuration_requires_no_peripheral_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "trip.toml"
            config_path.write_text("""
[trip]
start = 2030-06-10T11:00:00Z
end = 2030-06-13T11:00:00Z
timezone = "Etc/UTC"

[routine]
rooms = ["kitchen", "living"]
sunset = "19:50"
bedtime = "22:30"

[hardware.plugs]
kitchen = "192.168.1.20"
living = "192.168.1.21"
""")
            hardware = load_config(config_path).hardware
        with (
            patch.dict(sys.modules, {"rpi_ws281x": None}),
            patch("vacation_ghost.outputs.shutil.which", side_effect=AssertionError),
        ):
            outputs = PiOutputs(
                hardware.plugs, hardware.led_count, hardware.led_pin, hardware.sounds_dir, hardware.volume
            )
            self.assertFalse(outputs.check_glow())
            self.assertFalse(outputs.check_audio())
            outputs.set_glow((30, 20, 10))
            outputs.play_sound("tv")
            outputs.close()

    def test_plug_diagnostics_only_read_output(self):
        outputs = PiOutputs(self.plugs, led_count=30, volume=0.08)
        with (
            patch.dict(sys.modules, {"rpi_ws281x": None}),
            patch.object(outputs._http, "open", side_effect=[
                io.BytesIO(b'{"output":false}'), io.BytesIO(b'{"output":true}')
            ]) as open_request,
        ):
            self.assertFalse(outputs.lamp_status("kitchen"))
            self.assertTrue(outputs.lamp_status("living"))
            with self.assertRaisesRegex(RuntimeError, "Install rpi_ws281x"):
                outputs.check_glow()
            with self.assertRaisesRegex(ValueError, "hardware.sounds_dir"):
                outputs.check_audio()
        requests = [call.args[0] for call in open_request.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "GET"])
        self.assertEqual([request.selector for request in requests], ["/rpc/Switch.GetStatus?id=0"] * 2)

    def test_set_checks_the_output_after_switching(self):
        outputs = PiOutputs(self.plugs)
        with patch.object(outputs._http, "open", side_effect=[
            io.BytesIO(b'{"was_on":true}'), io.BytesIO(b'{"output":true}')
        ]) as open_request:
            with self.assertRaisesRegex(RuntimeError, "kitchen did not turn off"):
                outputs.set_lamp("kitchen", False)
        set_request, status_request = [call.args[0] for call in open_request.call_args_list]
        self.assertEqual(set_request.get_method(), "POST")
        self.assertEqual(set_request.selector, "/rpc/Switch.Set")
        self.assertEqual(json.loads(set_request.data), {"id": 0, "on": False})
        self.assertEqual(status_request.get_method(), "GET")
        with patch.object(outputs._http, "open", side_effect=[
            io.BytesIO(b'{"was_on":true}'), io.BytesIO(b'{"output":false}')
        ]):
            outputs.set_lamp("kitchen", False)

    def test_diagnostics_inspect_peripherals_without_setting_glow_or_playing_audio(self):
        pixel_strip = Mock()
        with tempfile.TemporaryDirectory() as directory:
            sounds_dir = Path(directory)
            for cue in SOUND_CUES:
                with wave.open(str(sounds_dir / f"{cue}.wav"), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\x01\x00")
            outputs = PiOutputs(self.plugs, 30, 18, sounds_dir, 0.08)
            with (
                patch.dict(sys.modules, {"rpi_ws281x": SimpleNamespace(Color=Mock(), PixelStrip=pixel_strip)}),
                patch("vacation_ghost.outputs.shutil.which", return_value="/usr/bin/aplay"),
                patch("vacation_ghost.outputs.subprocess.Popen") as play,
            ):
                self.assertTrue(outputs.check_glow())
                self.assertTrue(outputs.check_audio())
                (sounds_dir / "tv.wav").write_bytes(b"not a wave file")
                with self.assertRaisesRegex(ValueError, "tv.wav is not a readable PCM WAV"):
                    outputs.check_audio()
        pixel_strip.assert_not_called()
        play.assert_not_called()

    def test_strip_starts_on_first_glow_command(self):
        strip = Mock()
        strip.numPixels.return_value = 2
        pixel_strip = Mock(return_value=strip)
        color = Mock(return_value=12345)
        with patch.dict(sys.modules, {"rpi_ws281x": SimpleNamespace(Color=color, PixelStrip=pixel_strip)}):
            outputs = PiOutputs(self.plugs, led_count=2)
            pixel_strip.assert_not_called()
            outputs.set_glow((11, 15, 18))
        pixel_strip.assert_called_once_with(2, 18)
        strip.begin.assert_called_once_with()
        color.assert_called_once_with(11, 15, 18)
        self.assertEqual([call.args for call in strip.setPixelColor.call_args_list], [(0, 12345), (1, 12345)])
        strip.show.assert_called_once_with()

    def test_play_sound_caps_digital_amplitude(self):
        player = Mock()
        player.poll.return_value = 0

        def play(command, stdin):
            self.assertEqual(command, ["/usr/bin/aplay", "--quiet"])
            with wave.open(stdin, "rb") as wav:
                self.assertEqual(struct.unpack("<hh", wav.readframes(2)), (6553, -6553))
            return player

        with tempfile.TemporaryDirectory() as directory:
            sounds_dir = Path(directory)
            with wave.open(str(sounds_dir / "tv.wav"), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(struct.pack("<hh", 32767, -32768))
            outputs = PiOutputs(self.plugs, sounds_dir=sounds_dir, volume=0.20)
            with (
                patch("vacation_ghost.outputs.shutil.which", return_value="/usr/bin/aplay"),
                patch("vacation_ghost.outputs.subprocess.Popen", side_effect=play) as start_player,
            ):
                outputs.play_sound("tv")
                outputs.poll_audio()
        start_player.assert_called_once()
        player.wait.assert_not_called()

    def test_poll_audio_reports_failure_once_without_waiting(self):
        outputs = PiOutputs(self.plugs)
        player = Mock()
        player.poll.side_effect = [None, 1]
        outputs._player = player
        outputs.poll_audio()
        with self.assertRaisesRegex(RuntimeError, "aplay exited with status 1"):
            outputs.poll_audio()
        outputs.poll_audio()
        player.wait.assert_not_called()
