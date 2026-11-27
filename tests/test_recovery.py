import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from vacation_ghost import __main__ as cli
from vacation_ghost import reports as report_module
from vacation_ghost.config import Hardware, load_config
from vacation_ghost.engine import (
    SOUND_GRACE, Controller, PlugIdentityError, PlugPairingError, RehearsalIncomplete, Routine, rehearse, run,
)
from vacation_ghost.outputs import SimulationOutputs
from vacation_ghost.reports import ReportStore
from vacation_ghost.state import DeviceBusy, OutputReservation, lock_file


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vacation.example.toml"
TEST_HARDWARE_ID = "test-hardware"


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
        self.reservation = OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID)
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.now = self.config.end - timedelta(seconds=1)
        clocks = patch("vacation_ghost.engine.datetime")
        self.addCleanup(clocks.stop)
        clocks.start().now.side_effect = lambda _zone: self.now
        paths = patch.object(cli.state, "hardware_directory", side_effect=cli.state.private_directory)
        self.addCleanup(paths.stop)
        paths.start()

    def read(self, name):
        return json.loads((self.trip / name).read_text())

    def live_config(self):
        return replace(self.config, hardware=Hardware(
            plugs=dict(zip(self.config.rooms, ("192.168.0.10", "192.168.0.11"))),
            led_count=0, led_pin=18, sounds_dir=None, volume=0,
        ))

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

    def test_identity_failures_report_the_failed_start_without_switching_outputs(self):
        for failure, attempts in ((PlugIdentityError, 3), (PlugPairingError, 1)):
            with self.subTest(failure=failure.__name__):
                trip = self.root / failure.__name__
                reports = ReportStore(trip, self.config.start, self.config.end)
                identify = Mock(side_effect=failure("identity check failed for kitchen"))
                reservation = OutputReservation(trip, self.device, hardware_id=TEST_HARDWARE_ID, identify_plugs=identify)
                outputs = Mock(spec=SimulationOutputs)
                with self.assertRaises(failure), self.assertLogs(level="WARNING"):
                    run(self.config, Controller(self.config.rooms, outputs, reports), self.stop, reservation=reservation)
                self.assertEqual(identify.call_count, attempts)
                self.assertEqual(outputs.mock_calls, [])
                latest = json.loads((trip / "latest.json").read_text())
                self.assertEqual(latest["faults_by_device"], {"plug": attempts})
                self.assertIn("could not start", latest["message"])
                self.assertIn("kitchen", latest["startup_error"])
                self.assertFalse(latest["shutdown_unconfirmed"])
                self.assertFalse(reservation.engaged)

    def test_identity_retry_clears_startup_error_after_both_plugs_are_identified(self):
        outputs = SimulationOutputs()
        identify = Mock(side_effect=[PlugIdentityError("kitchen lookup failed"),
                                     dict(zip(self.config.rooms, ("000000000001", "000000000002")))])
        reservation = OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID, identify_plugs=identify)

        def wait(seconds):
            if seconds == 1:
                self.assertEqual(outputs.lamps, {})
                self.assertIn("kitchen", self.read("latest.json")["startup_error"])
            else:
                self.assertIsNone(self.read("latest.json")["startup_error"])
                self.now = self.config.end
            return False

        self.stop.wait.side_effect = wait
        with self.assertLogs(level="WARNING"):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop, reservation=reservation)
        self.assertEqual(identify.call_count, 2)
        self.assertEqual(self.read("trip.json")["faults_by_device"], {"plug": 1})

    def test_recovery_observes_trip_end_after_honoring_an_earlier_disarm(self):
        for early in (False, True):
            with self.subTest(early=early):
                trip = self.root / str(early)
                reports = ReportStore(trip, self.config.start, self.config.end)
                reservation = OutputReservation(trip)
                reservation.engage()
                reservation.confirm_shutdown()
                if early:
                    cli._request_disarm(trip / ".disarm-request.json", reports.trip_id, self.config.start)
                with patch.object(cli, "datetime") as clock:
                    clock.now.return_value = self.config.end + timedelta(hours=1)
                    cli._refresh_recovered_status(self.config, trip, reservation)
                latest = json.loads((trip / "latest.json").read_text())
                final = json.loads((trip / "trip.json").read_text())
                self.assertEqual((latest["completed"], latest["disarmed"]), (not early, early))
                self.assertEqual(final["ended_early"], early)
                self.assertEqual((trip / ".completion.json").exists(), not early)
                self.assertFalse(reservation.shutdown_confirmed)

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

    def test_switch_rechecks_scene_after_adapter_prepares_to_energize(self):
        bedtime = Routine(self.config).day(self.config.start.date()).events[-1].at
        self.now = bedtime - timedelta(milliseconds=500)
        outputs = SimulationOutputs()

        def delayed_switch(room, may_start):
            self.now += timedelta(seconds=1)
            if not may_start():
                return False
            outputs.set_lamp(room, True)
            return True

        self.stop.wait.side_effect = lambda _: setattr(self, "now", self.config.end)
        with (
            patch.object(outputs, "set_lamp_if", create=True, side_effect=delayed_switch) as guarded,
            patch.object(outputs, "set_lamp", wraps=outputs.set_lamp) as writes,
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual(guarded.call_count, 1)
        self.assertTrue(all(not on for _, on in (item.args for item in writes.call_args_list)))

    def test_glow_rechecks_scene_after_checkpointing_pending_output(self):
        plan = Routine(self.config).day(self.config.start.date())
        first_blackout = next(event.at for event in plan.events if event.kind == "glow" and not event.value)
        self.now = first_blackout - timedelta(milliseconds=500)
        record_pending = self.reservation.before_glow

        def checkpoint():
            record_pending()
            self.now += timedelta(seconds=1)

        self.stop.wait.side_effect = lambda _: setattr(self, "now", self.config.end)
        outputs = SimulationOutputs()
        with (
            patch.object(self.reservation, "before_glow", side_effect=checkpoint),
            patch.object(outputs, "set_glow", wraps=outputs.set_glow) as frames,
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual(frames.call_args_list, [call((0, 0, 0))] * 2)

    def test_sound_expires_during_adapter_preparation(self):
        cue = next(event for event in Routine(self.config).day(self.config.start.date()).events
                   if event.kind == "sound" and event.at > self.config.start)
        self.now = cue.at - timedelta(milliseconds=100)
        times = iter((cue.at, self.config.end))
        self.stop.wait.side_effect = lambda _: setattr(self, "now", next(times))
        outputs = SimulationOutputs()

        def delayed_play(name, may_start):
            self.now += SOUND_GRACE
            if not may_start():
                return False
            outputs.play_sound(name)
            return True

        with (
            patch.object(outputs, "play_sound_if", create=True, side_effect=delayed_play) as guarded,
            patch.object(outputs, "play_sound", wraps=outputs.play_sound) as played,
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual(guarded.call_count, 1)
        played.assert_not_called()

    def test_backward_clock_change_does_not_replay_a_sound_cue(self):
        cue = next(event for event in Routine(self.config).day(self.config.start.date()).events
                   if event.kind == "sound" and event.at > self.config.start)
        before, after = cue.at - timedelta(milliseconds=100), cue.at + timedelta(milliseconds=100)
        self.now = before
        times = iter((after, after + timedelta(seconds=2), before, after, self.config.end))
        self.stop.wait.side_effect = lambda _: setattr(self, "now", next(times))
        outputs = SimulationOutputs()
        with patch.object(outputs, "play_sound", wraps=outputs.play_sound) as played:
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        played.assert_called_once_with(cue.value)

    def test_heartbeat_restores_a_restarted_plug_without_overlapping_lamps(self):
        self.now = self.config.start
        kitchen, living = self.config.rooms
        outputs = SimulationOutputs()
        ticks = 0

        def advance(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                self.assertTrue(outputs.lamps[kitchen])
            elif ticks == 2:
                self.assertEqual(reads.call_args_list, [call(living), call(kitchen), call(living)])
                self.assertEqual(writes.call_count, 3)  # Startup OFF, OFF, then kitchen ON.
                outputs.lamps[kitchen] = False  # The active plug loses power.
                outputs.lamps[living] = True  # The other relay is on out of band.
            else:
                self.assertEqual(writes.call_args_list[-2:], [call(living, False), call(kitchen, True)])
                status = self.read("latest.json")
                self.assertTrue(status["active"])
                self.assertEqual(status["lamp_room"], kitchen)
                self.assertIsNone(status["relay_off_verified"])
                self.assertEqual(status["faults_by_device"], {"plug": 1})
                self.now = self.config.end
                return False
            self.now += timedelta(seconds=15)
            return False

        self.stop.wait.side_effect = advance
        with (
            patch.object(outputs, "set_lamp", wraps=outputs.set_lamp) as writes,
            patch.object(outputs, "lamp_status", wraps=outputs.lamp_status) as reads,
            patch("vacation_ghost.engine.monotonic", side_effect=lambda: self.now.timestamp()),
            patch("vacation_ghost.engine.Routine.desired", return_value=(kitchen, "", None)),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(self.read("trip.json")["faults_by_device"], {"plug": 1})

    def test_heartbeat_at_trip_end_waits_for_shutdown_result_before_final_report(self):
        outputs = SimulationOutputs()
        switch = outputs.set_lamp

        def delayed_switch(room, on):
            if on:
                self.now = self.config.end
            elif outputs.lamps.get(room):
                raise RuntimeError("relay did not turn off")
            switch(room, on)

        with (
            patch.object(outputs, "set_lamp", side_effect=delayed_switch),
            patch("vacation_ghost.engine.monotonic", return_value=1),
            patch("vacation_ghost.engine.sleep"),
            patch("vacation_ghost.engine.Routine.desired", return_value=(self.config.rooms[0], "", None)),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
            self.assertRaisesRegex(RuntimeError, "shutdown unconfirmed"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertTrue(self.reservation.engaged)
        self.assertTrue(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertTrue(self.read("trip.json")["shutdown_unconfirmed"])

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
            reservation=OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID))
        self.assertFalse(self.reservation.engaged)
        self.assertEqual(recovered.lamps, dict.fromkeys(self.config.rooms, False))
        self.assertFalse(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertTrue(self.read("trip.json")["shutdown_unconfirmed"])

    def test_physical_shutdown_retries_before_writing_faults(self):
        self.now = self.config.end
        self.reservation.engage()
        kitchen, living = self.config.rooms
        outputs = Mock(spec=SimulationOutputs)
        outputs.set_lamp.side_effect = [RuntimeError("plug offline"), None, None, None]
        with (
            patch("vacation_ghost.engine.sleep"),
            patch.object(self.reports, "record_device_fault", side_effect=OSError("disk unavailable")),
            self.assertRaisesRegex(OSError, "disk unavailable"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual([call.args for call in outputs.set_lamp.call_args_list],
                         [(kitchen, False), (living, False)] * 2)
        self.assertFalse(self.reservation.engaged)

    def test_restart_restores_shutdown_from_receipt_after_report_write_failure(self):
        self.now = self.config.end
        self.reservation.engage()
        self.reservation.before_glow()
        self.reports.record_device_fault(self.now, "plug", shutdown_unconfirmed=True)
        outputs = SimulationOutputs()
        write_report = report_module._write_json

        def fail_report_write(path, record):
            if path.name in {".activity.json", "latest.json"}:
                raise OSError("reports unavailable")
            write_report(path, record)

        with (
            patch.object(report_module, "_write_json", side_effect=fail_report_write),
            self.assertRaisesRegex(OSError, "reports unavailable"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertFalse(self.reservation.engaged)
        self.assertTrue(self.reservation.shutdown_confirmed)

        reports = ReportStore(self.trip, self.config.start, self.config.end)
        restarted = OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID)
        self.assertTrue(reports.shutdown_unconfirmed)
        with patch.object(outputs, "set_lamp", wraps=outputs.set_lamp) as writes:
            run(self.config, Controller(self.config.rooms, outputs, reports), self.stop, reservation=restarted)
        writes.assert_not_called()
        self.assertFalse(restarted.shutdown_confirmed)
        latest = self.read("latest.json")
        self.assertFalse(latest["shutdown_unconfirmed"])
        self.assertTrue(latest["relay_off_verified"])
        self.assertTrue(latest["led_black_frame_sent"])

    def test_receipt_remains_if_journal_succeeds_but_latest_status_write_fails(self):
        self.now = self.config.end
        self.reservation.engage()
        self.reports.record_device_fault(self.now, "plug", shutdown_unconfirmed=True)
        outputs = SimulationOutputs()
        write_report = report_module._write_json

        def fail_latest(path, record):
            if path.name == "latest.json":
                raise OSError("status unavailable")
            write_report(path, record)

        with (
            patch.object(report_module, "_write_json", side_effect=fail_latest),
            self.assertRaisesRegex(OSError, "status unavailable"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertFalse(self.read(".activity.json")["shutdown_unconfirmed"])
        self.assertTrue(self.reservation.shutdown_confirmed)

    def test_led_shutdown_failure_stays_unconfirmed_when_journal_write_fails(self):
        self.now = self.config.end
        self.reservation.engage()
        self.reservation.before_glow()
        outputs = Mock(spec=SimulationOutputs)
        outputs.set_glow.side_effect = RuntimeError("LED did not acknowledge off")
        write_report = report_module._write_json

        def fail_activity_write(path, record):
            if path.name == ".activity.json":
                raise OSError("activity journal unavailable")
            write_report(path, record)

        with (
            patch("vacation_ghost.engine.sleep"),
            patch.object(report_module, "_write_json", side_effect=fail_activity_write),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
            self.assertRaisesRegex(OSError, "activity journal unavailable"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.assertEqual(outputs.set_glow.call_args_list, [call((0, 0, 0))] * 3)
        self.assertTrue(self.reservation.engaged)
        self.assertTrue(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertTrue(self.read("latest.json")["relay_off_verified"])
        self.assertFalse(self.read("latest.json")["led_black_frame_sent"])
        self.assertTrue(self.read("trip.json")["shutdown_unconfirmed"])

    def test_legacy_marker_still_recovers_if_migration_write_fails(self):
        self.now = self.config.start
        legacy = OutputReservation(self.trip)
        legacy.engage()
        reservation = OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID)
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(reservation, "engage", side_effect=OSError("migration write failed")),
            self.assertRaisesRegex(OSError, "migration write failed"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=reservation)
        self.assertEqual(outputs.mock_calls, [
            *(call.set_lamp(room, False) for room in self.config.rooms),
            call.stop_sound(),
            call.set_glow((0, 0, 0)),
        ])
        self.assertFalse(legacy.engaged)

    def test_completion_is_durable_before_off_and_reports_survive_clock_rollback(self):
        self.now = self.config.end
        self.reservation.engage()
        outputs = Mock(spec=SimulationOutputs)

        def roll_back_on_first_off(_room, on):
            self.assertFalse(on)
            self.assertEqual(self.read(".completion.json")["completed_at"], self.config.end.isoformat())
            self.now = self.config.start

        outputs.set_lamp.side_effect = roll_back_on_first_off
        run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
            reservation=self.reservation)
        self.assertEqual(outputs.set_lamp.call_args_list, [call(room, False) for room in self.config.rooms])
        self.assertFalse(self.reservation.engaged)
        self.assertTrue(self.read("latest.json")["completed"])
        self.assertEqual(self.read("trip.json")["published_at"], self.config.end.isoformat())

    def test_plug_fault_at_deadline_still_persists_completion_before_off(self):
        self.now = self.config.start
        outputs = Mock(spec=SimulationOutputs)
        outputs.lamp_status.return_value = False

        def fail_on_at_deadline(_room, on):
            if on:
                self.now = self.config.end
                raise RuntimeError("lost relay acknowledgement")
            if self.now == self.config.end:
                self.assertEqual(self.read(".completion.json")["completed_at"], self.config.end.isoformat())
                self.now = self.config.start

        outputs.set_lamp.side_effect = fail_on_at_deadline
        with (
            patch("vacation_ghost.engine.Routine.desired", return_value=(self.config.rooms[0], "", None)),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
        ):
            run(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop,
                reservation=self.reservation)
        self.stop.wait.assert_not_called()
        self.assertFalse(self.reservation.engaged)
        self.assertEqual(self.read("trip.json")["faults_by_device"], {"plug": 1})
        self.assertFalse(self.read("trip.json")["shutdown_unconfirmed"])

    def test_stale_restart_recovers_pending_off_and_publishes_without_reactivation(self):
        self.reservation.engage()
        self.reports.complete(self.config.end)
        self.now = self.config.start
        restarted = ReportStore(self.trip, self.config.start, self.config.end)
        self.assertEqual(restarted.completed_at, self.config.end)
        self.assertFalse((self.trip / "trip.json").exists())
        outputs = Mock(spec=SimulationOutputs)
        run(self.config, Controller(self.config.rooms, outputs, restarted), self.stop,
            reservation=OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID))
        self.assertEqual(outputs.set_lamp.call_args_list, [call(room, False) for room in self.config.rooms])
        self.assertFalse(self.reservation.engaged)
        self.assertFalse(self.read("latest.json")["active"])
        self.assertEqual(self.read("trip.json")["published_at"], self.config.end.isoformat())

        outputs.reset_mock()
        run(self.config, Controller(self.config.rooms, outputs, restarted), self.stop,
            reservation=OutputReservation(self.trip, self.device, hardware_id=TEST_HARDWARE_ID),
            disarm_requested=lambda: self.config.start)
        self.assertEqual(outputs.mock_calls, [])
        self.assertFalse(self.read("latest.json")["disarmed"])

    def test_stale_restart_recognizes_existing_pre_upgrade_final_summary(self):
        self.reports.publish_due(self.config.end)
        existing = self.read("trip.json")
        self.assertFalse((self.trip / ".completion.json").exists())
        self.now = self.config.start
        restarted = ReportStore(self.trip, self.config.start, self.config.end)
        outputs = Mock(spec=SimulationOutputs)
        run(self.config, Controller(self.config.rooms, outputs, restarted), self.stop,
            reservation=self.reservation)
        self.assertEqual(outputs.mock_calls, [])
        self.assertEqual(self.read(".completion.json")["completed_at"], self.config.end.isoformat())
        self.assertEqual(self.read("trip.json"), existing)

    def test_malformed_completion_marker_fails_closed_and_still_recovers_pending_outputs(self):
        legacy = OutputReservation(self.trip)
        legacy.engage()
        marker = self.trip / ".completion.json"
        marker.write_text("{}", encoding="utf-8")
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            self.assertRaisesRegex(RuntimeError, "Shutdown confirmed.*invalid trip completion record"),
        ):
            cli._hardware_command("run", self.live_config(), self.trip)
        self.assertEqual(outputs.set_lamp.call_args_list, [call(room, False) for room in self.config.rooms])
        self.assertFalse(legacy.engaged)
        self.assertEqual(marker.read_text(encoding="utf-8"), "{}")

    def test_recover_command_never_activates_outputs_with_corrupt_report(self):
        config = self.live_config()
        legacy = OutputReservation(self.trip)
        legacy.engage()
        (self.trip / ".activity.json").write_text("invalid json", encoding="utf-8")
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            self.assertLogs(level="WARNING"),
            redirect_stdout(io.StringIO()) as output,
        ):
            cli._hardware_command("recover", config, self.trip)
        self.assertIn("Shutdown confirmed", output.getvalue())
        self.assertEqual([call.args for call in outputs.set_lamp.call_args_list],
                         [(room, False) for room in config.rooms])
        outputs.set_glow.assert_called_once_with((0, 0, 0))
        outputs.play_sound.assert_not_called()
        self.assertFalse(legacy.engaged)

    def test_startup_recovers_pending_outputs_before_surfacing_corrupt_report(self):
        config = self.live_config()
        legacy = OutputReservation(self.trip)
        legacy.engage()
        (self.trip / ".activity.json").write_text(json.dumps({"trip_id": self.reports.trip_id}), encoding="utf-8")
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            self.assertRaisesRegex(RuntimeError, "Shutdown confirmed. Could not open reports.*missing lamp"),
        ):
            cli._hardware_command("run", config, self.trip)
        self.assertEqual([call.args for call in outputs.set_lamp.call_args_list],
                         [(room, False) for room in config.rooms])
        outputs.play_sound.assert_not_called()
        self.assertFalse(legacy.engaged)

    def test_recover_requires_exclusive_hardware_lock_and_pending_shutdown(self):
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            redirect_stdout(io.StringIO()) as output,
        ):
            with lock_file(self.device / "hardware.lock"), self.assertRaises(DeviceBusy):
                cli._hardware_command("recover", self.live_config(), self.trip)
            cli._hardware_command("recover", self.live_config(), self.trip)
        self.assertIn("No pending shutdown", output.getvalue())
        outputs.set_lamp.assert_not_called()
        outputs.set_glow.assert_not_called()

    def test_recover_legacy_hardware_id_requires_original_led_backend(self):
        config = self.live_config()
        hardware = replace(config.hardware, led_count=2, led_pin=10)
        config = replace(config, hardware=hardware)
        legacy_settings = json.dumps([hardware.plugs, 2, 10], sort_keys=True)
        reservation = OutputReservation(
            self.trip, self.device, hardware_id=hashlib.sha256(legacy_settings.encode()).hexdigest()
        )
        reservation.before_glow()
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            redirect_stdout(io.StringIO()),
        ):
            changed = replace(config, hardware=replace(hardware, led_backend="spi"))
            with self.assertRaisesRegex(ValueError, "restore the original plug and LED configuration"):
                cli._hardware_command("recover", changed, self.trip)
            outputs.set_lamp.assert_not_called()
            outputs.set_glow.assert_not_called()
            self.assertTrue(reservation.engaged)
            cli._hardware_command("recover", config, self.trip)
        outputs.set_glow.assert_called_once_with((0, 0, 0))
        self.assertFalse(reservation.engaged)

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
        config = self.live_config()
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

    def test_disarm_with_unreadable_trip_owner_directs_operator_to_recover(self):
        (self.trip / ".trip.json").write_text("invalid json", encoding="utf-8")
        outputs = Mock(spec=SimulationOutputs)
        with (
            patch.object(cli.state, "DEVICE_DIR", self.device),
            patch.object(cli, "_outputs", return_value=outputs),
            self.assertRaisesRegex(RuntimeError, "Could not record disarm.*use recover"),
        ):
            cli._hardware_command("disarm", self.live_config(), self.trip)
        self.assertEqual(outputs.mock_calls, [call.close()])

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
        self.assertEqual(self.read("rehearsal.json")["outcome"], "failed")

    def test_partial_rehearsal_names_only_the_outputs_exercised_and_confirms_shutdown(self):
        def interrupt(_seconds):
            self.stop.is_set.return_value = True
            return True

        self.stop.wait.side_effect = interrupt
        outputs = SimulationOutputs()
        with self.assertRaisesRegex(RehearsalIncomplete, "Lamps confirmed on: kitchen"):
            rehearse(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop, self.reservation)
        record = self.read("rehearsal.json")
        self.assertEqual(record["outcome"], "interrupted")
        self.assertEqual(record["lamps_confirmed_on"], ["kitchen"])
        self.assertEqual(record["sound_cues_started"], ["kettle"])
        self.assertFalse(record["led_color_frame_sent"])
        self.assertTrue(record["relay_off_verified"])
        self.assertTrue(record["led_black_frame_sent"])
        self.assertFalse(any(outputs.lamps.values()))
        self.assertFalse(self.reservation.engaged)

    def test_cli_returns_a_distinct_status_for_an_incomplete_rehearsal(self):
        with (
            patch.object(cli, "_hardware_command", side_effect=RehearsalIncomplete("Rehearsal interrupted.")),
            redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as exit_status,
        ):
            cli.main(["--config", str(EXAMPLE), "rehearse", "--state-dir", str(self.trip)])
        self.assertEqual(exit_status.exception.code, 3)
        self.assertIn("Rehearsal interrupted", error.getvalue())

    def test_complete_lights_only_rehearsal_records_only_lamp_checks(self):
        outputs = SimulationOutputs()
        config = self.live_config()
        rehearse(config, Controller(config.rooms, outputs, self.reports), self.stop, self.reservation)
        record = self.read("rehearsal.json")
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["lamps_confirmed_on"], list(config.rooms))
        self.assertEqual(record["sound_cues_started"], [])
        self.assertFalse(record["led_color_frame_sent"])
        self.assertIsNone(record["led_black_frame_sent"])

    def test_rehearsal_preserves_shutdown_error_when_result_storage_also_fails(self):
        outputs = SimulationOutputs()
        self.stop.is_set.return_value = True
        with (
            patch.object(outputs, "set_lamp", side_effect=OSError("relay offline")),
            patch.object(self.reports, "write_rehearsal", side_effect=OSError("no storage")),
            patch("vacation_ghost.engine.sleep"),
            self.assertLogs(level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "shutdown unconfirmed"),
        ):
            rehearse(self.config, Controller(self.config.rooms, outputs, self.reports), self.stop, self.reservation)
        self.assertTrue(self.reservation.engaged)
