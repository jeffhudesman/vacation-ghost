import unittest
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from vacation_ghost.schedule import build_day


class DayPlanTests(unittest.TestCase):
    day = date(2030, 6, 10)
    tz = ZoneInfo("Europe/Berlin")
    rooms = ("kitchen", "living_room")

    def test_restarts_repeat_the_plan_and_evenings_vary_by_date(self):
        options = {"sunset_fallback": time(16, 35), "bedtime": time(23)}
        today = build_day(self.day, self.tz, self.rooms, **options)
        tomorrow = build_day(self.day + timedelta(days=1), self.tz, self.rooms, **options)
        self.assertEqual(today, build_day(self.day, self.tz, self.rooms, **options))
        today_evening = [e.at.time() for e in today.events if e.at.hour >= 15]
        tomorrow_evening = [e.at.time() for e in tomorrow.events if e.at.hour >= 15]
        self.assertNotEqual(today_evening, tomorrow_evening)
        self.assertNotEqual(today_evening[-1], tomorrow_evening[-1])

    def test_evening_follows_estimated_local_sunset(self):
        plan = build_day(
            self.day, self.tz, self.rooms, sunset_fallback=time(19), bedtime=time(23),
            latitude=52.5200, longitude=13.4050,
        )
        expected = datetime.combine(self.day, time(21, 27), self.tz)
        self.assertLessEqual(abs(plan.sunset - expected), timedelta(minutes=5))
        first_evening = next(event for event in plan.events if event.at.hour >= 15)
        self.assertLessEqual(abs(first_evening.at - plan.sunset), timedelta(minutes=16))

    def test_polar_day_uses_configured_sunset_fallback(self):
        tz = ZoneInfo("Europe/Oslo")
        plan = build_day(
            self.day, tz, self.rooms, sunset_fallback=time(16, 30), bedtime=time(23),
            latitude=69.6492, longitude=18.9553,
        )
        self.assertEqual(plan.sunset, datetime.combine(self.day, time(16, 30), tz))

    def test_morning_activity_leaves_the_middle_of_the_day_quiet(self):
        plan = build_day(self.day, self.tz, self.rooms, sunset_fallback=time(16, 35), bedtime=time(23))
        morning = [event for event in plan.events if event.at.hour < 9]
        self.assertEqual((morning[0].kind, morning[0].value), ("lamp", self.rooms[0]))
        self.assertIn(("sound", "kettle"), [(event.kind, event.value) for event in morning])
        self.assertEqual((morning[-1].kind, morning[-1].value), ("lamp", ""))
        self.assertFalse(any(9 <= event.at.hour < 15 for event in plan.events))

    def test_full_evening_keeps_lamp_and_scene_events_in_order(self):
        for offset in range(14):
            day = self.day + timedelta(days=offset)
            with self.subTest(day=day):
                plan = build_day(day, self.tz, self.rooms, sunset_fallback=time(21), bedtime=time(23, 55))
                self.assertIsNone(plan.evening_note)
                self.assertEqual([event.at for event in plan.events], sorted(event.at for event in plan.events))
                self.assertTrue(all(event.at.date() == day for event in plan.events))
                self.assertLessEqual(plan.events[-1].at.time(), time(23, 55))
                lamp = glow = ""
                scene_changes = []
                for event in plan.events:
                    if event.kind == "lamp":
                        self.assertIn(event.value, ("", *self.rooms))
                        lamp = event.value
                    elif event.kind == "glow":
                        self.assertIn(event.value, ("", "drama", "sports"))
                        glow = event.value
                        scene_changes.append(bool(glow))
                    if glow:
                        self.assertEqual(lamp, self.rooms[1])
                self.assertEqual(scene_changes, [True, False, True, False])
                self.assertEqual((lamp, glow), ("", ""))

    def test_late_calculated_sunset_shortens_evening_without_backdating_it(self):
        day = date(2030, 6, 21)
        plan = build_day(day, ZoneInfo("Europe/Oslo"), self.rooms, sunset_fallback=time(18), bedtime=time(23, 20),
                         latitude=59.9139, longitude=10.7522)
        evening = [event for event in plan.events if event.at.hour >= 15]
        self.assertAlmostEqual(plan.sunset.hour * 60 + plan.sunset.minute, 22 * 60 + 44, delta=4)
        self.assertLessEqual(abs(evening[0].at - plan.sunset), timedelta(minutes=16))
        self.assertLess(evening[-1].at - evening[0].at, timedelta(minutes=75))
        self.assertEqual([bool(event.value) for event in evening if event.kind == "glow"], [True, False])
        self.assertEqual((evening[-1].kind, evening[-1].value), ("lamp", ""))
        self.assertIn("shortened", plan.evening_note)

    def test_calculated_sunset_after_bedtime_skips_evening_only(self):
        day = date(2030, 6, 21)
        plan = build_day(day, ZoneInfo("Europe/Oslo"), self.rooms, sunset_fallback=time(18), bedtime=time(22, 15),
                         latitude=59.9139, longitude=10.7522)
        self.assertTrue(plan.events)
        self.assertTrue(all(event.at.hour < 9 for event in plan.events))
        self.assertIn("skipped", plan.evening_note)

    def test_spring_clock_gap_preserves_cue_order_and_uses_real_times(self):
        day = date(2026, 9, 5)
        tz = ZoneInfo("Pacific/Easter")
        plan = build_day(day, tz, self.rooms, sunset_fallback=time(22), bedtime=time(23, 55))
        self.assertEqual(plan.sunset, datetime.combine(day, time(23), tz))
        instants = [event.at.astimezone(UTC) for event in plan.events]
        self.assertEqual(instants, sorted(instants))
        for event, instant in zip(plan.events, instants):
            self.assertEqual(event.at.date(), day)
            self.assertNotEqual(event.at.hour, 22)
            self.assertEqual(event.at.isoformat(), instant.astimezone(tz).isoformat())
        scene_changes = [bool(event.value) for event in plan.events if event.kind == "glow"]
        self.assertEqual(scene_changes, [True, False])
        self.assertEqual((plan.events[-1].kind, plan.events[-1].value), ("lamp", ""))

    def test_evening_still_ends_before_midnight_when_bedtime_is_skipped(self):
        day = date(2026, 3, 28)
        tz = ZoneInfo("America/Nuuk")
        plan = build_day(day, tz, self.rooms, sunset_fallback=time(20), bedtime=time(23, 55))
        self.assertTrue(all(event.at.date() == day for event in plan.events))
        self.assertLess(plan.events[-1].at.time(), time(23))
        self.assertEqual((plan.events[-1].kind, plan.events[-1].value), ("lamp", ""))

    def test_fall_clock_fold_orders_both_occurrences_of_the_repeated_hour(self):
        day = date(2026, 4, 4)
        tz = ZoneInfo("Pacific/Easter")
        plan = build_day(day, tz, self.rooms, sunset_fallback=time(21, 30), bedtime=time(22, 30))
        self.assertEqual(plan.sunset.astimezone(UTC), datetime(2026, 4, 5, 2, 30, tzinfo=UTC))
        self.assertEqual({event.at.fold for event in plan.events if event.at.hour == 21}, {0, 1})
        instants = [event.at.astimezone(UTC) for event in plan.events]
        self.assertEqual(instants, sorted(instants))
        self.assertEqual(sum(event.kind == "sound" and event.value == "tv" for event in plan.events), 1)
        self.assertEqual((plan.events[-1].kind, plan.events[-1].value), ("lamp", ""))
