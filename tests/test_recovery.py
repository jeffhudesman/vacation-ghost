import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vacation_ghost import __main__ as cli
from vacation_ghost.config import Hardware, load_config
from vacation_ghost.engine import Controller, rehearse, run
from vacation_ghost.outputs import SimulationOutputs
from vacation_ghost.reports import ReportStore
from vacation_ghost.state import DeviceBusy, OutputReservation, lock_file


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vacation.example.toml"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.trip = self.root / "trip"
        self.device = self.root / "device"
        self.device.mkdir()
        self.config = load_config(EXAMPLE)
        self.reports = ReportStore(self.trip, self.config.start, self.config.end)
        self.reservation = OutputReservation(self.trip, self.device)
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.now = self.config.end - timedelta(seconds=1)
        clocks = patch("vacation_ghost.engine.datetime")
        self.addCleanup(clocks.stop)
        clocks.start().now.side_effect = lambda _zone: self.now

    def read(self, name):
        return json.loads((self.trip / name).read_text())

    def test_slow_startup_does_not_energize_after_deadline(self):
        outputs = Mock(spec=SimulationOutputs)
        def set_lamp(_room, on):
            self.assertFalse(on, "activation after the trip deadline")
            self.now += timedelta(seconds=1)
        outputs.set_lamp.side_effect = set_lamp
        self.stop.wait.side_effect = lambda _seconds: False
        with patch("vacation_ghost.engine.Routine.desired", return_value=(self.config.rooms[0], "drama", "tv")):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        outputs.play_sound.assert_not_called()
        self.assertFalse(self.reservation.engaged)
        self.assertFalse(self.read("trip.json")["shutdown_unconfirmed"])

    def test_switch_waits_for_previous_off_and_rechecks_deadline(self):
        outputs = Mock(spec=SimulationOutputs)
        controller = Controller(self.config.rooms, outputs, self.reports)
        kitchen, living = self.config.rooms
        controller.lamp(self.now, kitchen)
        outputs.reset_mock()
        outputs.set_lamp.side_effect = lambda _room, _on: setattr(self, "now", self.config.end)
        controller.lamp(self.now, living, lambda: self.now < self.config.end)
        outputs.set_lamp.assert_called_once_with(kitchen, False)
        self.assertIsNone(controller.room)

    def test_unconfirmed_shutdown_recovers_after_restart(self):
        self.now = self.config.end
        self.reservation.engage()
        outputs = Mock(spec=SimulationOutputs)
        outputs.set_lamp.side_effect = RuntimeError("plug offline")
        with patch("vacation_ghost.engine.sleep"):
            with self.assertRaisesRegex(RuntimeError, "shutdown unconfirmed"):
                run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                    reservation=self.reservation)
        self.assertTrue(self.reservation.engaged)
        self.assertFalse(self.read("latest.json")["active"])
        self.assertTrue(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertTrue(self.read("trip.json")["shutdown_unconfirmed"])

        restarted = ReportStore(self.trip, self.config.start, self.config.end)
        recovered = SimulationOutputs()
        run(self.config, Controller(self.config.rooms, recovered, restarted), self.stop,
            reservation=OutputReservation(self.trip, self.device))
        self.assertFalse(self.reservation.engaged)
        self.assertEqual(recovered.lamps, dict.fromkeys(self.config.rooms, False))
        self.assertFalse(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertTrue(self.read("trip.json")["shutdown_unconfirmed"])

    def test_transient_plug_failure_reconciles_and_resumes(self):
        self.now = self.config.start
        outputs = SimulationOutputs()
        on_requests = 0
        switch = outputs.set_lamp

        def set_lamp(room, on):
            nonlocal on_requests
            if on:
                on_requests += 1
                if on_requests == 1:
                    raise RuntimeError("lost acknowledgment")
                self.assertFalse(any(outputs.lamps.values()))
            switch(room, on)

        def wait(seconds):
            self.now += timedelta(seconds=seconds)
            if on_requests == 2:
                self.now = self.config.end

        self.stop.wait.side_effect = wait
        with (
            patch.object(outputs, "set_lamp", side_effect=set_lamp),
            patch("vacation_ghost.engine.Routine.desired", return_value=(self.config.rooms[0], "", None)),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual(on_requests, 2)
        self.assertEqual(self.read("trip.json")["faults_by_device"], {"plug": 1})
        self.assertFalse(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertFalse(any(outputs.lamps.values()))

    def test_device_reservation_blocks_other_trips_and_changed_hardware(self):
        with lock_file(self.device / "hardware.lock"):
            with self.assertRaises(DeviceBusy), lock_file(self.device / "hardware.lock"):
                self.fail("the same device lock was acquired twice")
        reservation = OutputReservation(self.trip, self.device, hardware_id="original")
        reservation.engage()
        with self.assertRaisesRegex(RuntimeError, "shutdown is still pending"):
            OutputReservation(self.root / "another-trip", self.device, hardware_id="original")
        with self.assertRaisesRegex(ValueError, "restore the original"):
            OutputReservation(self.trip, self.device, hardware_id="changed")

    def test_disarm_command_queues_while_device_is_busy_and_survives_restart(self):
        outputs = SimulationOutputs()
        config = replace(self.config, hardware=Hardware(
            plugs=dict(zip(self.config.rooms, ("192.168.0.10", "192.168.0.11"))),
            led_count=0, led_pin=18, sounds_dir=None, volume=0,
        ))
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            patch.object(cli, "_stop_event", return_value=self.stop),
            patch("vacation_ghost.__main__.datetime") as clock,
            redirect_stdout(io.StringIO()),
        ):
            clock.now.return_value = self.now
            with lock_file(self.device / "hardware.lock"):
                cli._hardware_command("disarm", config, self.trip)
            self.assertTrue((self.trip / ".disarm-request.json").exists())
            self.assertFalse((self.trip / "trip.json").exists())
            cli._hardware_command("run", config, self.trip)
            cli._hardware_command("run", config, self.trip)
        self.assertEqual(outputs.lamps, {})
        self.assertTrue(self.read("latest.json")["disarmed"])
        self.assertTrue(self.read("trip.json")["ended_early"])

    def test_rehearsal_continues_lamps_when_audio_fails(self):
        case = self
        class QuietOutputs(SimulationOutputs):
            activated = []
            def set_lamp(self, room, on):
                if on:
                    case.assertFalse(any(self.lamps.values()))
                    self.activated.append(room)
                super().set_lamp(room, on)
            def play_sound(self, _cue):
                raise ValueError("malformed WAV")

        outputs = QuietOutputs()
        with self.assertRaisesRegex(RuntimeError, "sound"):
            rehearse(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                     self.reservation)
        self.assertEqual(tuple(outputs.activated), self.config.rooms)
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertFalse(self.reservation.engaged)
