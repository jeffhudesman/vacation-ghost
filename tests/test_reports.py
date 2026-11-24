import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vacation_ghost.reports import ReportStore, trip_identity


class ReportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.start = datetime(2030, 6, 10, 9, tzinfo=UTC)
        self.end = self.start + timedelta(days=3)

    def store(self):
        return ReportStore(self.state, self.start, self.end)

    def read(self, filename):
        return json.loads((self.state / filename).read_text(encoding="utf-8"))

    def test_unchanged_status_can_refresh_heartbeat(self):
        reports = self.store()
        reports.status(self.start, False, None)
        initial = self.read("latest.json")
        later = self.start + timedelta(seconds=15)
        reports.status(later, False, None)
        self.assertEqual(self.read("latest.json"), initial)
        reports.status(later, False, None, force=True)
        self.assertEqual(self.read("latest.json")["updated_at"], later.isoformat())

    def test_fault_history_survives_restart_and_reports_recovered_shutdown(self):
        reports = self.store()
        reports.record_device_fault(self.start + timedelta(hours=9), "glow")
        reports.record_device_fault(self.start + timedelta(hours=10), shutdown_unconfirmed=True)
        reports = self.store()
        reports.status(self.start + timedelta(hours=11), True, None)
        latest = self.read("latest.json")
        self.assertEqual(latest["faults_by_device"], {"glow": 1, "plug": 1})
        self.assertTrue(latest["shutdown_unconfirmed"])
        reports.confirm_shutdown()
        reports.status(self.start + timedelta(hours=12), True, None)
        self.assertFalse(self.read("latest.json")["shutdown_unconfirmed"])

        reports.publish_due(self.start + timedelta(hours=23))
        daily = self.read("daily/2030-06-11.json")
        self.assertEqual(daily["device_faults"], 2)
        self.assertTrue(daily["shutdown_unconfirmed"])
        self.assertIn("shutdown could not be confirmed", daily["message"])

        reports.publish_due(self.end)
        final = self.read("trip.json")
        self.assertEqual(final["device_faults"], 2)
        self.assertFalse(final["shutdown_unconfirmed"])

    def test_disarm_survives_restart_and_closes_trip_at_effective_time(self):
        reports = self.store()
        evening = self.start + timedelta(hours=9)
        reports.lamp_changed(evening, "kitchen")
        reports.status(evening + timedelta(minutes=1), True, "kitchen")
        stopped = evening + timedelta(minutes=3)
        self.assertEqual(reports.disarm(stopped), stopped)
        reports.lamp_changed(stopped + timedelta(seconds=2), None)
        reports.record_device_fault(stopped + timedelta(seconds=2), "shutdown", shutdown_unconfirmed=True)

        reports = self.store()
        self.assertEqual(reports.disarmed_at, stopped)
        final_time = stopped + timedelta(seconds=5)
        self.assertEqual(reports.disarm(final_time), stopped)
        reports.status(final_time, False, None)
        reports.publish_due(final_time)
        latest, final = self.read("latest.json"), self.read("trip.json")
        self.assertTrue(latest["disarmed"])
        self.assertTrue(final["ended_early"])
        self.assertTrue(final["shutdown_unconfirmed"])
        self.assertEqual(final["lamp_seconds"], 180)
        self.assertEqual(final["trip_id"], trip_identity(self.start, self.end))
        self.assertIn("disarmed early", final["message"])

        reports.confirm_shutdown()
        reports.status(self.end, False, None)
        reports.publish_due(self.end)
        self.assertFalse(self.read("latest.json")["shutdown_unconfirmed"])
        self.assertEqual(self.read("trip.json"), final)
        self.assertEqual(list((self.state / "daily").iterdir()), [])

    def test_trip_disarmed_before_departure_has_zero_runtime(self):
        reports = self.store()
        stopped = self.start - timedelta(minutes=5)
        reports.disarm(stopped)
        reports.publish_due(stopped)
        final = self.read("trip.json")
        self.assertTrue(final["ended_early"])
        self.assertEqual(final["lamp_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
