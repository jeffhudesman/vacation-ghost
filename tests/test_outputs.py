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
            patch("vacation_ghost.outputs._rpc_request", side_effect=[
                b'{"output":false}', b'{"output":true}'
            ]) as rpc_request,
        ):
            self.assertFalse(outputs.lamp_status("kitchen"))
            self.assertTrue(outputs.lamp_status("living"))
            with self.assertRaisesRegex(RuntimeError, "Install rpi_ws281x"):
                outputs.check_glow()
            with self.assertRaisesRegex(ValueError, "hardware.sounds_dir"):
                outputs.check_audio()
        self.assertEqual(
            [call.args for call in rpc_request.call_args_list],
            [
                ("http://192.168.1.20", "Switch.GetStatus?id=0", None),
                ("http://192.168.1.21", "Switch.GetStatus?id=0", None),
            ],
        )

    def test_set_checks_the_output_after_switching(self):
        outputs = PiOutputs(self.plugs)
        with patch("vacation_ghost.outputs._rpc_request", side_effect=[
            b'{"was_on":true}', b'{"output":true}'
        ]) as rpc_request:
            with self.assertRaisesRegex(RuntimeError, "kitchen did not turn off"):
                outputs.set_lamp("kitchen", False)
        set_request, status_request = [call.args for call in rpc_request.call_args_list]
        self.assertEqual(set_request[:2], ("http://192.168.1.20", "Switch.Set"))
        self.assertEqual(json.loads(set_request[2]), {"id": 0, "on": False})
        self.assertEqual(status_request, ("http://192.168.1.20", "Switch.GetStatus?id=0", None))
        with patch("vacation_ghost.outputs._rpc_request", side_effect=[
            b'{"was_on":true}', b'{"output":false}'
        ]):
            outputs.set_lamp("kitchen", False)

    def test_plugs_accept_only_direct_lan_origins(self):
        for origin in ("127.0.0.1", "169.254.0.1", "8.8.8.8", "plug.local", "[fe80::1]"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                PiOutputs({"kitchen": origin})
        PiOutputs({"kitchen": "192.168.1.20", "living": "https://[fd00::2]:443"})

    def test_rpc_deadline_bounds_the_whole_response(self):
        header = b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\n"
        body = b'{"output":false}'
        for name, chunks in (
            ("complete", [header + body]),
            ("partial headers", [header[:20], header[20:40], header[40:] + body]),
            ("partial body", [header, body[:10], body[10:]]),
        ):
            with self.subTest(name=name):
                now = 100.0
                timeout = None
                sock = Mock()
                stream = sock.makefile.return_value
                wire_chunks = iter(chunks)

                def settimeout(seconds):
                    nonlocal timeout
                    timeout = seconds

                def connect(_address):
                    nonlocal now
                    now += 0.4

                def sendall(_request):
                    nonlocal now
                    now += 0.3

                def readinto(buffer):
                    nonlocal now
                    read_seconds = 0.5
                    if timeout < read_seconds:
                        now += timeout
                        raise TimeoutError("timed out")
                    now += read_seconds
                    chunk = next(wire_chunks)
                    buffer[:len(chunk)] = chunk
                    return len(chunk)

                sock.settimeout.side_effect = settimeout
                sock.connect.side_effect = connect
                sock.sendall.side_effect = sendall
                stream.readinto.side_effect = readinto
                outputs = PiOutputs(self.plugs)
                with (
                    patch("vacation_ghost.outputs.monotonic", side_effect=lambda: now),
                    patch("vacation_ghost.outputs.socket.socket", return_value=sock) as make_socket,
                ):
                    if name == "complete":
                        self.assertFalse(outputs.lamp_status("kitchen"))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "timed out"):
                            outputs.lamp_status("kitchen")
                        self.assertAlmostEqual(now, 102.0)
                make_socket.assert_called_once()
                sock.connect.assert_called_once_with(("192.168.1.20", 80))
                self.assertIn(b"GET /rpc/Switch.GetStatus?id=0 HTTP/1.1", sock.sendall.call_args.args[0])
                stream.close.assert_called()
                sock.close.assert_called()

    def test_https_spends_the_same_budget_and_uses_default_tls_verification(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n" + b'{"output":false}'
        now = 100.0
        tcp = Mock()
        tls = Mock()
        context = Mock()

        def connect(_address):
            nonlocal now
            now += 0.5

        def wrap_socket(sock, *, server_hostname):
            nonlocal now
            self.assertIs(sock, tcp)
            self.assertEqual(server_hostname, "fd00::2")
            self.assertEqual(tcp.settimeout.call_args.args, (1.5,))
            now += 0.5
            return tls

        def readinto(buffer):
            buffer[:len(response)] = response
            return len(response)

        tcp.connect.side_effect = connect
        context.wrap_socket.side_effect = wrap_socket
        tls.makefile.return_value.readinto.side_effect = readinto
        outputs = PiOutputs({"kitchen": "https://[fd00::2]:8443"})
        with (
            patch("vacation_ghost.outputs.monotonic", side_effect=lambda: now),
            patch("vacation_ghost.outputs.socket.socket", return_value=tcp),
            patch("vacation_ghost.outputs.ssl.create_default_context", return_value=context) as make_context,
        ):
            self.assertFalse(outputs.lamp_status("kitchen"))
        make_context.assert_called_once_with()
        tcp.connect.assert_called_once_with(("fd00::2", 8443))
        self.assertEqual(tls.settimeout.call_args_list[0].args, (1.0,))
        self.assertIn(b"Host: [fd00::2]:8443", tls.sendall.call_args.args[0])

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
