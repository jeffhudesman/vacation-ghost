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

    def test_corrupt_daily_report_is_preserved_without_blocking_later_reports(self):
        reports = self.store()
        broken = self.state / "daily" / "2030-06-11.json"
        original = b"{\"message\": \xff}"
        broken.write_bytes(original)
        with self.assertLogs("vacation_ghost.reports", "WARNING") as warning:
            reports.publish_due(self.end)

        archives = list(broken.parent.glob(f"{broken.name}.corrupt-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), original)
        self.assertIn("Preserved malformed report", warning.output[0])
        current = self.read("daily/2030-06-11.json")
        self.assertEqual(current["id"], f"{reports.trip_id}:2030-06-11")
        self.assertEqual(self.read("daily/2030-06-12.json")["id"], f"{reports.trip_id}:2030-06-12")
        self.assertEqual(self.read("trip.json")["id"], f"{reports.trip_id}:trip")

        self.store().publish_due(self.end + timedelta(hours=1))
        self.assertEqual(self.read("daily/2030-06-11.json"), current)
        self.assertEqual(list(broken.parent.glob(f"{broken.name}.corrupt-*")), archives)

    def test_structurally_invalid_final_report_is_preserved_and_regenerated(self):
        reports = self.store()
        invalid = self.state / "trip.json"
        original = b"[\"not a report\"]\n"
        invalid.write_bytes(original)
        with self.assertLogs("vacation_ghost.reports", "WARNING"):
            reports.publish_due(self.end)
        archives = list(self.state.glob(f"{invalid.name}.corrupt-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), original)
        self.assertEqual(self.read("trip.json")["id"], f"{reports.trip_id}:trip")

    def test_report_path_access_error_is_not_treated_as_corruption(self):
        reports = self.store()
        unrelated = self.state / "unrelated.txt"
        unrelated.write_text("keep this content", encoding="utf-8")
        report = self.state / "daily" / "2030-06-11.json"
        report.symlink_to(unrelated)

        with self.assertRaises(OSError):
            reports.publish_due(self.end)
        self.assertTrue(report.is_symlink())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep this content")
        self.assertEqual(list(report.parent.glob(f"{report.name}.corrupt-*")), [])


if __name__ == "__main__":
    unittest.main()
