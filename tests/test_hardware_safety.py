import hashlib
import json
from dataclasses import replace
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from vacation_ghost import __main__ as cli
from vacation_ghost.config import load_config
from vacation_ghost.engine import PlugPairingError, RehearsalIncomplete
from vacation_ghost.reports import ReportStore
from vacation_ghost.state import OutputReservation


class HardwareSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.trip = Path(directory) / "trip"
        self.device = Path(directory) / "device"
        self.device.mkdir()
        self.config = load_config(Path(__file__).resolve().parents[1] / "examples" / "vacation.lights-only.toml")
        hardware = self.config.hardware
        settings = [hardware.plugs, hardware.led_count, hardware.led_pin]
        self.hardware_id = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        self.addresses = {address: room for room, address in hardware.plugs.items()}
        self.identities = {"kitchen": "001122334455", "living": "001122334466"}
        self.relays = dict.fromkeys(self.identities, True)
        self.writes = []
        self.rpc = self.enterContext(patch("vacation_ghost.outputs._rpc_request", side_effect=self._rpc))
        self.enterContext(patch.object(cli.state, "DEVICE_DIR", self.device))
        self.enterContext(patch.object(cli.state, "hardware_directory", side_effect=cli.state.private_directory))
        self.enterContext(patch.object(cli, "_stop_event", side_effect=Event))
        self.enterContext(patch("vacation_ghost.engine.sleep"))

    def _rpc(self, address, method, data, *, can_send=None):
        room = self.addresses[address]
        if method == "Shelly.GetDeviceInfo":
            response = {"mac": self.identities[room], "gen": 4}
        elif method == "Switch.GetStatus?id=0":
            response = {"output": self.relays[room]}
        else:
            self.assertEqual(method, "Switch.Set")
            if can_send is not None and not can_send():
                return None
            response = {"was_on": self.relays[room]}
            self.relays[room] = json.loads(data)["on"]
            self.writes.append((room, self.relays[room]))
        return json.dumps(response).encode()

    def reserve_outputs(self):
        self.trip.mkdir(exist_ok=True)
        reservation = OutputReservation(
            self.trip, self.device, hardware_id=self.hardware_id, identify_plugs=self.identities.copy,
        )
        reservation.engage()
        return reservation

    def test_run_and_rehearse_reject_two_addresses_for_one_plug_before_switching(self):
        cli._pair(self.config, replace=False)
        self.identities["living"] = self.identities["kitchen"]
        with patch("vacation_ghost.engine.datetime") as clock:
            clock.now.return_value = self.config.start + timedelta(seconds=1)
            for command in ("run", "rehearse"):
                with self.subTest(command=command), self.assertRaisesRegex(RuntimeError, "same Shelly device"):
                    cli._hardware_command(command, self.config, self.trip / command)
        self.assertEqual(self.writes, [])
        self.assertFalse((self.device / "outputs.json").exists())

    def test_idle_service_cannot_pass_commissioning_without_a_pairing(self):
        with patch.object(cli, "datetime") as clock, self.assertRaisesRegex(PlugPairingError, "paired rooms"):
            clock.now.return_value = self.config.start - timedelta(days=1)
            cli._hardware_command("run", self.config, self.trip)
        self.rpc.assert_not_called()
        self.assertFalse((self.trip / "latest.json").exists())

    def test_replacement_requires_explicit_pairing_after_clean_shutdown(self):
        with self.assertRaisesRegex(PlugPairingError, "No paired plugs"):
            cli._hardware_command("rehearse", self.config, self.trip)
        cli._pair(self.config, replace=False)
        binding = self.device / "paired-plugs.json"
        original = binding.read_bytes()
        self.assertEqual(binding.stat().st_mode & 0o777, 0o600)
        reservation = self.reserve_outputs()
        with self.assertRaisesRegex(RuntimeError, "pending shutdown"):
            cli._pair(self.config, replace=True)
        cli._hardware_command("recover", self.config, self.trip)
        self.assertFalse(reservation.engaged)
        self.writes.clear()
        self.identities["kitchen"] = "001122334477"
        with self.assertRaisesRegex(PlugPairingError, "pairing changed"):
            cli._hardware_command("rehearse", self.config, self.trip)
        with self.assertRaisesRegex(PlugPairingError, "pair --replace"):
            cli._pair(self.config, replace=False)
        self.assertEqual(binding.read_bytes(), original)
        cli._pair(self.config, replace=True)
        self.assertEqual(json.loads(binding.read_text()), self.identities)
        self.assertEqual(self.writes, [])

    def test_unknown_setting_prevents_actuation_after_recovering_pending_shutdown(self):
        for command in ("rehearse", "run"):
            with self.subTest(command=command):
                self.trip = self.device.parent / command
                self.reserve_outputs()
                self.writes.clear()
                config = replace(self.config, unknown_settings=("hardware.volum",))
                with self.assertRaisesRegex(ValueError, "hardware.volum"):
                    cli._hardware_command(command, config, self.trip)
                self.assertEqual(self.writes, [("kitchen", False), ("living", False)])
                self.assertFalse((self.device / "outputs.json").exists())

    def test_recovery_preserves_pending_state_and_attempts_other_relay_on_mac_mismatch(self):
        reservation = self.reserve_outputs()
        original_mac = self.identities["kitchen"]
        self.identities["kitchen"] = "001122334477"
        with self.assertLogs(level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "shutdown unconfirmed"):
                cli._hardware_command("recover", self.config, self.trip)
        self.assertTrue(reservation.engaged)
        self.assertTrue(self.relays["kitchen"])
        self.assertFalse(self.relays["living"])
        self.assertTrue(self.writes)
        self.assertTrue(all(write == ("living", False) for write in self.writes))

        self.identities["kitchen"] = original_mac
        cli._hardware_command("recover", self.config, self.trip)
        self.assertFalse(reservation.engaged)
        self.assertFalse(self.relays["kitchen"])

    def test_legacy_reservation_migrates_off_before_binding_plugs_for_rehearsal(self):
        cli._pair(self.config, replace=False)
        self.rpc.reset_mock()
        self.trip.mkdir()
        legacy = OutputReservation(self.trip, self.device, hardware_id=self.hardware_id)
        legacy.engage()
        stop = Event()
        stop.set()
        with (
            patch.object(cli, "_stop_event", return_value=stop), self.assertLogs(level="WARNING") as logs,
            self.assertRaises(RehearsalIncomplete),
        ):
            cli._hardware_command("rehearse", self.config, self.trip)
        self.assertIn("Legacy shutdown record", "\n".join(logs.output))
        operations = [call.args[1] for call in self.rpc.call_args_list]
        self.assertEqual(operations[:6], [
            "Switch.Set", "Switch.GetStatus?id=0", "Switch.Set", "Switch.GetStatus?id=0",
            "Shelly.GetDeviceInfo", "Shelly.GetDeviceInfo",
        ])
        self.assertTrue(all(not on for _, on in self.writes))
        self.assertFalse(legacy.engaged)
        self.assertFalse((self.trip / ".shutdown-confirmed.json").exists())

    def test_recovery_can_retry_reporting_without_contacting_plugs_again(self):
        reservation = self.reserve_outputs()
        reports = ReportStore(self.trip, self.config.start, self.config.end)
        reports.record_device_fault(self.config.start, "shutdown", shutdown_unconfirmed=True)
        reports.status(self.config.start, False, None)
        latest_path = self.trip / "latest.json"

        with patch.object(ReportStore, "status", side_effect=OSError("write failed")), self.assertLogs(level="WARNING"):
            cli._hardware_command("recover", self.config, self.trip)
        self.assertFalse(reservation.engaged)
        self.assertTrue(json.loads(latest_path.read_text())["shutdown_unconfirmed"])
        self.assertTrue((self.trip / ".shutdown-confirmed.json").is_file())

        self.rpc.side_effect = AssertionError("report refresh tried to contact a plug")
        cli._hardware_command("recover", self.config, self.trip)
        latest = json.loads(latest_path.read_text())
        self.assertFalse(latest["shutdown_unconfirmed"])
        self.assertTrue(latest["relay_off_verified"])
        self.assertFalse((self.trip / ".shutdown-confirmed.json").exists())
