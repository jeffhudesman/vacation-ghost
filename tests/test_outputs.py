import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from vacation_ghost import __main__ as cli
from vacation_ghost.config import load_config
from vacation_ghost.outputs import MAX_SOUND_BYTES, PiOutputs, SOUND_CUES, SOUND_STOP_TIMEOUT


LIGHTS_ONLY = Path(__file__).resolve().parents[1] / "examples" / "vacation.lights-only.toml"


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
        for origin in ("127.0.0.1", "169.254.0.1", "8.8.8.8", "plug.local", "fe80::1", "fd00::2%eth0"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                PiOutputs({"kitchen": origin})
        PiOutputs({"kitchen": "192.168.1.20", "living": "https://[fd00::2]:443"})

    def test_bare_and_bracketed_ipv6_canonicalize_to_same_origin(self):
        outputs = PiOutputs({"kitchen": "fd00:0:0:0::2"})
        with patch("vacation_ghost.outputs._rpc_request", return_value=b'{"output":false}') as rpc:
            self.assertFalse(outputs.lamp_status("kitchen"))
        rpc.assert_called_once_with("http://[fd00::2]", "Switch.GetStatus?id=0", None)
        with self.assertRaisesRegex(ValueError, "Each room needs its own plug"):
            PiOutputs({"kitchen": "fd00::2", "living": "http://[fd00::2]:80"})

    def test_device_identity_requires_valid_shelly_mac(self):
        outputs = PiOutputs(self.plugs)
        for response in (b"{}", b'{"mac":false}', b'{"mac":"not-a-mac"}'):
            with (
                self.subTest(response=response),
                patch("vacation_ghost.outputs._rpc_request", return_value=response),
                self.assertRaisesRegex(RuntimeError, "valid device MAC for kitchen"),
            ):
                outputs.plug_identity("kitchen")

    def test_rpc_encodes_switch_post_and_confirms_with_get(self):
        sockets = []
        for body in (b'{"was_on":false}', b'{"output":true}'):
            sock = Mock()
            headers = (
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            sock.makefile.return_value = io.BytesIO(headers + body)
            sockets.append(sock)
        with patch("vacation_ghost.outputs.socket.socket", side_effect=sockets):
            PiOutputs(self.plugs).set_lamp("kitchen", True)
        set_socket, status_socket = sockets
        written = b"".join(request.args[0] for request in set_socket.sendall.call_args_list)
        headers, body = written.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"POST /rpc/Switch.Set HTTP/1.1\r\n"))
        self.assertIn(b"Content-Type: application/json", headers)
        self.assertIn(f"Content-Length: {len(body)}".encode("ascii"), headers)
        self.assertEqual(json.loads(body), {"id": 0, "on": True})
        self.assertIn(
            b"GET /rpc/Switch.GetStatus?id=0 HTTP/1.1", status_socket.sendall.call_args.args[0]
        )

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
        pixel_strip.assert_called_once_with(2, 18, channel=0)
        strip.begin.assert_called_once_with()
        color.assert_called_once_with(11, 15, 18)
        self.assertEqual([call.args for call in strip.setPixelColor.call_args_list], [(0, 12345), (1, 12345)])
        strip.show.assert_called_once_with()

    def test_glow_maps_channel_one_and_rejects_invalid_pins_when_enabled(self):
        driver = Mock()
        driver.PixelStrip.return_value.numPixels.return_value = 0
        with patch("vacation_ghost.outputs._load_glow_driver", return_value=(driver.Color, driver.PixelStrip)):
            PiOutputs(self.plugs, led_count=2, led_pin=19).set_glow((0, 0, 0))
        driver.PixelStrip.assert_called_once_with(2, 19, channel=1)
        with self.assertRaisesRegex(ValueError, "Use BCM 18"):
            PiOutputs(self.plugs, led_count=2, led_pin=17)
        PiOutputs(self.plugs, led_pin=17)

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

    def test_sound_rejects_nonregular_symlinked_and_oversized_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cue = path / "tv.wav"
            outputs = PiOutputs(self.plugs, sounds_dir=path, volume=0.08)
            with (
                patch("vacation_ghost.outputs.shutil.which", return_value="/usr/bin/aplay"),
                patch("vacation_ghost.outputs.subprocess.Popen") as start_player,
            ):
                os.mkfifo(cue)
                with self.assertRaisesRegex(ValueError, "tv.wav must be a regular file"):
                    outputs.play_sound("tv")
                cue.unlink()
                other_file = path / "other.wav"
                other_file.touch()
                cue.symlink_to(other_file)
                with self.assertRaises(OSError):
                    outputs.play_sound("tv")
                cue.unlink()
                with cue.open("wb") as oversized:
                    oversized.truncate(MAX_SOUND_BYTES + 1)
                with self.assertRaisesRegex(ValueError, "exceeds the 8 MiB cue size limit"):
                    outputs.play_sound("tv")
        start_player.assert_not_called()

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

    def test_stop_sound_bounds_both_waits_and_can_be_retried(self):
        outputs = PiOutputs(self.plugs)
        player = Mock()
        player.poll.return_value = None
        timeout = subprocess.TimeoutExpired("aplay", SOUND_STOP_TIMEOUT)
        player.wait.side_effect = [timeout, timeout, 0]
        outputs._player = player
        with self.assertRaisesRegex(RuntimeError, "aplay did not stop after being killed"):
            outputs.stop_sound()
        self.assertEqual(
            player.wait.call_args_list, [call(timeout=SOUND_STOP_TIMEOUT)] * 2
        )
        player.terminate.assert_called_once_with()
        player.kill.assert_called_once_with()
        outputs.stop_sound()
        self.assertIsNone(outputs._player)


class CliDiagnosticsTest(unittest.TestCase):
    def test_doctor_confirms_distinct_devices_without_logging_identities(self):
        responses = [
            b'{"mac":"AABBCCDDEE01"}', b'{"output":false}',
            b'{"mac":"AABBCCDDEE02"}', b'{"output":true}',
        ]
        output = io.StringIO()
        with patch("vacation_ghost.outputs._rpc_request", side_effect=responses) as rpc, redirect_stdout(output):
            exit_code = cli.main(["--config", str(LIGHTS_ONLY), "doctor"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "Plug kitchen: off", "Plug living: on", "TV glow: disabled", "Sound: disabled",
        ])
        self.assertEqual([entry.args[1:] for entry in rpc.call_args_list], [
            ("Shelly.GetDeviceInfo", None), ("Switch.GetStatus?id=0", None),
            ("Shelly.GetDeviceInfo", None), ("Switch.GetStatus?id=0", None),
        ])

    def test_doctor_rejects_two_origins_for_one_device(self):
        config = load_config(LIGHTS_ONLY)
        config = replace(config, hardware=replace(config.hardware, plugs={
            "kitchen": "http://192.168.1.60", "living": "https://192.168.1.60",
        }))
        responses = [b'{"mac":"AABBCCDDEE01"}', b'{"output":false}', b'{"mac":"aabbccddee01"}']
        output = io.StringIO()
        with (
            patch.object(cli, "load_config", return_value=config),
            patch("vacation_ghost.outputs._rpc_request", side_effect=responses),
            redirect_stdout(output),
        ):
            exit_code = cli.main(["--config", str(LIGHTS_ONLY), "doctor"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Plug living: failed (same Shelly device as kitchen. Configure two distinct plugs.)", output.getvalue())
        self.assertNotIn("aabbccddee01", output.getvalue().lower())

    def test_simulate_at_error_explains_format_and_missing_offset(self):
        for value in ("not a date", "2030-06-11T09:00:00"):
            error = io.StringIO()
            with self.subTest(value=value), redirect_stderr(error), self.assertRaises(SystemExit) as exit_code:
                cli.main(["--config", str(LIGHTS_ONLY), "simulate", "--state-dir", "unused", "--at", value])
            self.assertEqual(exit_code.exception.code, 2)
            self.assertIn("ISO 8601 date and time with a UTC offset", error.getvalue())
            self.assertIn("2030-06-11T09:00:00Z", error.getvalue())

    def test_disarm_command_reports_damaged_request_without_rewriting_it(self):
        config = load_config(LIGHTS_ONLY)
        outputs = Mock(spec=PiOutputs)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            path = state_dir / ".disarm-request.json"
            original = json.dumps({"trip_id": cli.trip_identity(config.start, config.end)})
            path.write_text(original)
            error = io.StringIO()
            with (
                patch.object(cli, "_outputs", return_value=outputs),
                patch.object(cli.state, "DEVICE_DIR", state_dir / "device"),
                patch.object(cli.state, "hardware_directory", side_effect=cli.state.private_directory),
                redirect_stderr(error),
                self.assertRaises(SystemExit) as exit_code,
            ):
                cli.main(["--config", str(LIGHTS_ONLY), "disarm", "--state-dir", directory])
            self.assertEqual(exit_code.exception.code, 1)
            self.assertIn("requested_at must be an ISO 8601 timestamp with a UTC offset", error.getvalue())
            self.assertIn(str(path), error.getvalue())
            self.assertIn("use recover", error.getvalue())
            self.assertEqual(path.read_text(), original)
        outputs.set_lamp.assert_not_called()
