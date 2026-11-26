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

    def load_led_config(self, settings):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "trip.toml"
            config_path.write_text(LIGHTS_ONLY.read_text().replace("led_count = 0", settings))
            return load_config(config_path)

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
        self.assertEqual((hardware.led_backend, hardware.led_pin), ("rpi_ws281x", 18))
        with (
            patch.dict(sys.modules, {"rpi_ws281x": None, "spidev": None}),
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

    def test_config_selects_glow_backend_and_pin(self):
        for settings, backend, pin, count in (
            ("led_count = 2", "rpi_ws281x", 18, 2),
            ("led_count = 2\nled_pin = 19", "rpi_ws281x", 19, 2),
            ('led_backend = "spi"\nled_count = 144', "spi", 10, 144),
        ):
            with self.subTest(backend=backend, pin=pin, count=count):
                hardware = self.load_led_config(settings).hardware
                self.assertEqual((hardware.led_backend, hardware.led_pin, hardware.led_count), (backend, pin, count))

    def test_config_and_outputs_reject_incompatible_spi_settings(self):
        for settings in (
            'led_backend = "spi"\nled_count = 2\nled_pin = 18',
            'led_backend = "spi"\nled_count = 145',
            'led_backend = "other"\nled_count = 2',
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.load_led_config(settings)
        with self.assertRaises(ValueError):
            PiOutputs(self.plugs, led_backend="spi", led_count=2, led_pin=18)
        with self.assertRaises(ValueError):
            PiOutputs(self.plugs, led_backend="spi", led_count=145)

    def test_disabled_spi_glow_requires_no_driver_or_device(self):
        with patch.dict(sys.modules, {"spidev": None, "rpi_ws281x": None}):
            outputs = PiOutputs(self.plugs, led_backend="spi")
            self.assertFalse(outputs.check_glow())
            outputs.set_glow((1, 2, 3))
            outputs.set_glow((0, 0, 0))
            outputs.close()

    def test_doctor_checks_spi_device_without_writing_a_frame(self):
        config = self.load_led_config('led_backend = "spi"\nled_count = 2')
        device = Mock()
        factory = Mock(return_value=device)
        responses = [
            b'{"mac":"AABBCCDDEE01"}', b'{"output":false}',
            b'{"mac":"AABBCCDDEE02"}', b'{"output":true}',
        ]
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"spidev": SimpleNamespace(SpiDev=factory), "rpi_ws281x": None}),
            patch("vacation_ghost.outputs._rpc_request", side_effect=responses),
            redirect_stdout(output),
        ):
            self.assertEqual(cli._doctor(config), 0)
        self.assertIn("TV glow: driver available", output.getvalue())
        factory.assert_called_once_with()
        device.open.assert_called_once_with(0, 0)
        device.writebytes.assert_not_called()
        device.close.assert_called_once_with()

    def test_spi_glow_writes_one_grb_frame_and_blacks_out_from_a_fresh_instance(self):
        device, recovered_device = Mock(), Mock()
        factory = Mock(side_effect=[device, recovered_device])
        with patch.dict(sys.modules, {"spidev": SimpleNamespace(SpiDev=factory), "rpi_ws281x": None}):
            outputs = PiOutputs(self.plugs, led_backend="spi", led_count=2)
            factory.assert_not_called()
            outputs.set_glow((0x40, 0x80, 0x01))
            outputs.close()
            recovery = PiOutputs(self.plugs, led_backend="spi", led_count=144)
            recovery.set_glow((0, 0, 0))
            recovery.close()

        reset = b"\x00" * 256
        green = b"\xf0" + b"\xc0" * 7
        red = b"\xc0\xf0" + b"\xc0" * 6
        blue = b"\xc0" * 7 + b"\xf0"
        device.writebytes.assert_called_once()
        self.assertEqual(bytes(device.writebytes.call_args.args[0]), reset + (green + red + blue) * 2 + reset)
        recovered_device.writebytes.assert_called_once()
        self.assertEqual(bytes(recovered_device.writebytes.call_args.args[0]), reset + b"\xc0" * (24 * 144) + reset)
        for spi_device in (device, recovered_device):
            spi_device.open.assert_called_once_with(0, 0)
            spi_device.close.assert_called_once_with()
            self.assertEqual((spi_device.max_speed_hz, spi_device.bits_per_word, spi_device.mode), (6_400_000, 8, 0))
            self.assertFalse(spi_device.lsbfirst)

    def test_spi_glow_reports_missing_driver_or_device(self):
        outputs = PiOutputs(self.plugs, led_backend="spi", led_count=1)
        with patch.dict(sys.modules, {"spidev": None}):
            with self.assertRaisesRegex(RuntimeError, "spidev"):
                outputs.check_glow()
        device = Mock()
        device.open.side_effect = FileNotFoundError(2, "No such file or directory", "/dev/spidev0.0")
        with patch.dict(sys.modules, {"spidev": SimpleNamespace(SpiDev=Mock(return_value=device))}):
            with self.assertRaisesRegex((OSError, RuntimeError), "/dev/spidev0.0"):
                outputs.check_glow()
            with self.assertRaisesRegex((OSError, RuntimeError), "/dev/spidev0.0"):
                outputs.set_glow((0, 0, 0))
        device.writebytes.assert_not_called()

    def test_cli_recover_retries_spi_blackout_after_failed_rehearsal(self):
        black_frame = b"\x00" * 256 + b"\xc0" * 24 + b"\x00" * 256
        relays = dict.fromkeys((f"http://{ip}" for ip in self.plugs.values()), False)
        strip_lit = False

        def rpc_request(origin, operation, data=None):
            if operation == "Switch.Set":
                previous = relays[origin]
                relays[origin] = json.loads(data)["on"]
                return json.dumps({"was_on": previous}).encode()
            self.assertEqual(operation, "Switch.GetStatus?id=0")
            return json.dumps({"output": relays[origin]}).encode()

        def fail_blackout(frame):
            nonlocal strip_lit
            if bytes(frame) != black_frame:
                strip_lit = True
            elif strip_lit:
                raise OSError("simulated SPI write failure")

        faulty, recovered = Mock(), Mock()
        faulty.writebytes.side_effect = fail_blackout
        factory = Mock(return_value=faulty)
        stop = Mock()
        stop.is_set.return_value = stop.wait.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path = path / "trip.toml"
            config_path.write_text(LIGHTS_ONLY.read_text().replace("led_count = 0", 'led_backend = "spi"\nled_count = 1'))
            state_dir = path / "state"
            device_dir = path / "device"
            reservation = device_dir / "outputs.json"
            with (
                patch.dict(sys.modules, {"spidev": SimpleNamespace(SpiDev=factory), "rpi_ws281x": None}),
                patch.object(cli.state, "DEVICE_DIR", device_dir),
                patch.object(cli.state, "hardware_directory", side_effect=cli.state.private_directory),
                patch.object(cli, "_stop_event", return_value=stop),
                patch("vacation_ghost.outputs._rpc_request", side_effect=rpc_request),
                patch("vacation_ghost.engine.sleep"),
                redirect_stderr(io.StringIO()) as error,
                redirect_stdout(io.StringIO()) as output,
            ):
                command = ["--config", str(config_path)]
                state_args = ["--state-dir", str(state_dir)]
                with self.assertRaises(SystemExit) as exit_code:
                    cli.main([*command, "rehearse", *state_args])
                self.assertEqual(exit_code.exception.code, 1)
                self.assertIn("shutdown unconfirmed", error.getvalue())
                self.assertTrue(strip_lit)
                self.assertFalse(any(relays.values()))
                self.assertTrue(json.loads(reservation.read_text())["glow_pending"])

                factory.return_value = recovered
                recovered.writebytes.side_effect = lambda _: self.assertTrue(reservation.is_file())
                self.assertEqual(cli.main([*command, "recover", *state_args]), 0)
                self.assertIn("Shutdown confirmed.", output.getvalue())
                recovered.open.assert_called_once_with(0, 0)
                recovered.writebytes.assert_called_once_with(list(black_frame))
                recovered.close.assert_called_once_with()
                self.assertFalse(reservation.exists())
                self.assertFalse(any(relays.values()))

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
