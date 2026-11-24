import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("Sound detection was off", first["message"])
        summary = self.read("trip.json")
        self.assertEqual(summary["nights_with_lamp_activity"], 3)
        self.assertFalse(self.read("latest.json")["active"])
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertIsNone(outputs.active_sound)

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
        with patch("vacation_ghost.engine.datetime", Clock), patch("vacation_ghost.engine.build_day", return_value=plan):
            run(config, Controller(config.rooms, outputs, reports), Clock)
        self.assertEqual(outputs.rooms, [kitchen, living])
        self.assertEqual(outputs.cues, ["kettle"])
        self.assertFalse(any(outputs.lamps.values()))
        self.assertEqual(outputs.glow, (0, 0, 0))
        self.assertIsNone(outputs.active_sound)
        self.assertTrue((self.state / "trip.json").exists())
        self.assertFalse((self.state / ".outputs-active").exists())


if __name__ == "__main__":
    unittest.main()
