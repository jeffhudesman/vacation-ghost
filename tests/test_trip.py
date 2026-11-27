import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from vacation_ghost.__main__ import main
from vacation_ghost.config import load_config
from vacation_ghost.engine import Controller, run, simulate
from vacation_ghost.outputs import SimulationOutputs
from vacation_ghost.reports import ReportStore
from vacation_ghost.schedule import DayPlan, Event


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vacation.example.toml"


class TripTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(EXAMPLE)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)

    def read(self, name):
        return json.loads((self.state / name).read_text())

    def test_replay_to_trip_end(self):
        reports = ReportStore(self.state, self.config.start, self.config.end, dry_run=True)
        outputs = SimulationOutputs()
        simulate(self.config, outputs, reports, self.config.end)
        daily = sorted((self.state / "daily").glob("*.json"))
        self.assertEqual(len(daily), 3)
        self.assertEqual(daily[0].name, "2030-06-11.json")
        first = self.read("daily/2030-06-11.json")
        self.assertEqual(first["night_of"], "2030-06-10")
        self.assertGreater(first["lamp_seconds"], 0)
        self.assertTrue(first["dry_run"])
        self.assertNotIn("Sound detection", first["message"])
        summary = self.read("trip.json")
        self.assertEqual(summary["nights_with_lamp_activity"], 3)
        self.assertNotIn("Sound detection", summary["message"])
        latest = self.read("latest.json")
        self.assertFalse(latest["active"])
        self.assertTrue(latest["completed"])
        self.assertIn("finished the trip", latest["message"])
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertIsNone(outputs.active_sound)

    def test_wrong_fixed_sunset_requires_a_config_change_but_allows_loading_for_shutdown(self):
        path = self.state / "late-sunset.toml"
        path.write_text(EXAMPLE.read_text().replace('sunset = "19:40"', 'sunset = "22:00"')
                        .replace('bedtime = "23:00"', 'bedtime = "18:00"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "sunset must be before routine.bedtime"):
            load_config(path)
        for command in ("recover", "disarm"):
            with patch("vacation_ghost.__main__._hardware_command") as hardware:
                self.assertEqual(main(["--config", str(path), command, "--state-dir", str(self.state)]), 0)
                hardware.assert_called_once()
                self.assertEqual(hardware.call_args.args[1].sunset, time(22))

    def test_simulation_honors_lights_only_settings_and_retains_the_showcase(self):
        lights = load_config(EXAMPLE.with_name("vacation.lights-only.toml"))
        for config in (lights, self.config):
            for complete in (False, True):
                enabled = config.hardware is None
                with self.subTest(enabled=enabled, complete=complete):
                    through = config.end if complete else config.start.replace(hour=23, minute=55)
                    state = self.state / f"{enabled}-{complete}"
                    reports = ReportStore(state, config.start, config.end, dry_run=True)
                    outputs = SimulationOutputs()
                    with (
                        patch.object(outputs, "play_sound", wraps=outputs.play_sound) as sounds,
                        patch.object(outputs, "set_glow", wraps=outputs.set_glow) as frames,
                    ):
                        simulate(config, outputs, reports, through)
                    latest = json.loads((state / "latest.json").read_text())
                    self.assertIs(latest["led_black_frame_sent"], True if enabled else None)
                    self.assertEqual(sounds.called, enabled)
                    self.assertEqual(any(any(call.args[0]) for call in frames.call_args_list), enabled)

    def test_replay_reports_skipped_late_evening(self):
        zone = ZoneInfo("Europe/Oslo")
        config = replace(self.config, timezone=zone, start=datetime(2030, 6, 21, 12, tzinfo=zone),
                         end=datetime(2030, 6, 22, 8, tzinfo=zone), sunset=time(18), bedtime=time(22, 15),
                         latitude=59.9139, longitude=10.7522)
        reports = ReportStore(self.state, config.start, config.end, dry_run=True)
        simulate(config, SimulationOutputs(), reports, config.end)
        daily = self.read("daily/2030-06-22.json")
        self.assertEqual(daily["lamp_seconds"], 0)
        self.assertIn("skipped because sunset", daily["schedule_note"])
        self.assertIn(daily["schedule_note"], daily["message"])

    def test_daily_due_at_eight_and_restart_does_not_invent_runtime(self):
        reports = ReportStore(self.state, self.config.start, self.config.end)
        evening = self.config.start.replace(hour=20)
        reports.lamp_changed(evening, self.config.rooms[0])
        reports.status(evening + timedelta(minutes=2), True, self.config.rooms[0])

        reports = ReportStore(self.state, self.config.start, self.config.end)
        morning = (evening + timedelta(days=1)).replace(hour=8)
        daily = self.state / "daily/2030-06-11.json"
        reports.publish_due(morning - timedelta(seconds=1))
        self.assertFalse(daily.exists())
        reports.publish_due(morning)
        contents = daily.read_bytes()
        self.assertEqual(json.loads(contents)["lamp_seconds"], 120)

        reports = ReportStore(self.state, self.config.start, self.config.end)
        reports.publish_due(morning + timedelta(hours=1))
        self.assertEqual(daily.read_bytes(), contents)

    def test_live_trip_arms_switches_and_shuts_down(self):
        start = self.config.start
        end = start + timedelta(seconds=4)
        config = replace(self.config, end=end)
        reports = ReportStore(self.state, start, end)
        case = self

        class Clock:
            now_at = start - timedelta(seconds=1)

            @classmethod
            def now(cls, _zone):
                return cls.now_at

            @classmethod
            def wait(cls, seconds):
                cls.now_at += timedelta(seconds=seconds)

            @staticmethod
            def is_set():
                return False

        class CheckedOutputs(SimulationOutputs):
            def __init__(self):
                super().__init__()
                self.cues = []
                self.rooms = []

            def set_lamp(self, room, on):
                case.assertGreaterEqual(Clock.now_at, start)
                if on:
                    case.assertLess(Clock.now_at, end)
                    case.assertFalse(any(self.lamps.values()))
                    self.rooms.append(room)
                super().set_lamp(room, on)

            def play_sound(self, cue):
                self.cues.append(cue)
                super().play_sound(cue)

        kitchen, living = config.rooms
        plan = DayPlan(start.date(), start, (
            Event(start, "lamp", kitchen),
            Event(start + timedelta(seconds=1), "sound", "kettle"),
            Event(start + timedelta(seconds=2), "lamp", living),
            Event(start + timedelta(seconds=2), "glow", "drama"),
        ))
        outputs = CheckedOutputs()
        with patch("vacation_ghost.engine.datetime", Clock), patch("vacation_ghost.engine.build_config_day", return_value=plan):
            run(config, Controller(config.rooms, outputs, reports), Clock)
        self.assertEqual(outputs.rooms, [kitchen, living])
        self.assertEqual(outputs.cues, ["kettle"])
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertIsNone(outputs.active_sound)
        self.assertTrue((self.state / "trip.json").exists())
        self.assertFalse((self.state / ".outputs-active").exists())

    def test_live_transitions_record_relay_acknowledgments(self):
        start = self.config.start
        now = start

        class DelayedOutputs(SimulationOutputs):
            def set_lamp(self, room, on):
                nonlocal now
                now += timedelta(seconds=2)
                super().set_lamp(room, on)

        reports = ReportStore(self.state, start, self.config.end)
        controller = Controller(self.config.rooms, DelayedOutputs(), reports)
        kitchen, living = self.config.rooms
        controller.lamp(now, living, clock=lambda: now)
        now = start + timedelta(minutes=1)
        controller.lamp(now, kitchen, clock=lambda: now)
        now = start + timedelta(minutes=2)
        controller.off(now, clock=lambda: now)
        reports.publish_due(self.config.end)
        summary = self.read("trip.json")
        self.assertEqual(summary["first_lamp_activity_at"], (start + timedelta(seconds=2)).isoformat())
        self.assertEqual(summary["last_lamp_activity_at"], (start + timedelta(seconds=122)).isoformat())
        self.assertEqual(summary["lamp_seconds"], 118)

    def test_reconciles_unexpected_relays_during_active_and_quiet_periods(self):
        reports = ReportStore(self.state, self.config.start, self.config.end)
        outputs = SimulationOutputs()
        controller = Controller(self.config.rooms, outputs, reports)
        kitchen, living = self.config.rooms
        now = self.config.start
        def clock():
            return now

        controller.lamp(now, kitchen, clock=clock)
        outputs.lamps[living] = True
        with self.assertLogs("vacation_ghost.engine", "WARNING"):
            controller.verify_lamps(clock)
        self.assertEqual(outputs.lamps, {kitchen: True, living: False})

        controller.lamp(now, None, clock=clock)
        outputs.lamps[kitchen] = outputs.lamps[living] = True
        with self.assertLogs("vacation_ghost.engine", "WARNING"):
            controller.verify_lamps(clock)
        self.assertEqual(outputs.lamps, dict.fromkeys(self.config.rooms, False))

        outputs.lamps[living] = True
        with patch.object(outputs, "set_lamp", wraps=outputs.set_lamp) as switches:
            with self.assertLogs("vacation_ghost.engine", "WARNING"):
                controller.lamp(now, kitchen, clock=clock)
        self.assertEqual([switch.args for switch in switches.call_args_list], [(living, False), (kitchen, True)])

    def test_led_failure_retries_darkness_without_resuming_glow(self):
        for failed_blackouts, pending_plug in ((1, False), (1, True), (3, False)):
            with self.subTest(failed_blackouts=failed_blackouts, pending_plug=pending_plug):
                class FlakyGlow(SimulationOutputs):
                    color_attempts = 0
                    dark_attempts = 0

                    def set_glow(self, rgb):
                        if any(rgb):
                            self.color_attempts += 1
                            if self.color_attempts == 2:
                                raise RuntimeError("frame failed")
                        else:
                            self.dark_attempts += 1
                            if self.dark_attempts <= failed_blackouts:
                                raise RuntimeError("blackout failed")
                        super().set_glow(rgb)

                now = self.config.start
                tick = 0
                outputs = FlakyGlow()
                reports = ReportStore(self.state / f"{failed_blackouts}-{pending_plug}", now, self.config.end)
                if pending_plug:
                    reports.record_device_fault(now, "plug", shutdown_unconfirmed=True)
                controller = Controller(self.config.rooms, outputs, reports)
                controller.random.seed(42)
                with patch("vacation_ghost.engine.monotonic", side_effect=lambda: tick):
                    controller.update_glow(now, "drama")
                    self.assertNotEqual(outputs.glow, (0, 0, 0))
                    with self.assertLogs("vacation_ghost.engine", "WARNING"):
                        controller.update_glow(now, "sports")
                        self.assertTrue(reports.shutdown_unconfirmed)
                        for tick in (0.5, 1, 1.5, 3.5, 4, 300):
                            controller.update_glow(now + timedelta(seconds=tick), "")
                            controller.update_glow(now + timedelta(seconds=tick), "sports")
                self.assertEqual(outputs.color_attempts, 2)
                self.assertEqual(outputs.dark_attempts, min(3, failed_blackouts + 1))
                self.assertEqual(reports.shutdown_unconfirmed, failed_blackouts == 3 or pending_plug)
                if failed_blackouts == 1:
                    self.assertEqual(outputs.glow, (0, 0, 0))

    def test_startup_blackout_retry_clears_uncertainty_after_other_outputs_confirm_off(self):
        now = self.config.start
        reports = ReportStore(self.state, now, self.config.end)
        reports.record_device_fault(now, "shutdown", shutdown_unconfirmed=True)
        outputs = SimulationOutputs()
        outputs.set_glow((1, 3, 5))
        controller = Controller(self.config.rooms, outputs, reports)
        set_glow = outputs.set_glow
        failed = False

        def fail_first_blackout(rgb):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("blackout failed")
            set_glow(rgb)

        with (
            patch.object(outputs, "set_glow", side_effect=fail_first_blackout) as writes,
            patch("vacation_ghost.engine.monotonic", side_effect=[0, 1]),
            self.assertLogs("vacation_ghost.engine", "WARNING"),
        ):
            self.assertFalse(controller.off(now, glow_pending=True))
            self.assertTrue(reports.shutdown_unconfirmed)
            controller.update_glow(now + timedelta(seconds=1), "drama")
        self.assertEqual([write.args for write in writes.call_args_list], [((0, 0, 0),), ((0, 0, 0),)])
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertFalse(reports.shutdown_unconfirmed)


if __name__ == "__main__":
    unittest.main()
