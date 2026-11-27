import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vacation_ghost.reports import ReportStore, trip_identity
from vacation_ghost.state import InvalidState


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
        self.assertIn("earlier fault", daily["message"])
        self.assertIn("latest.json", daily["message"])

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

    def test_full_simulation_reports_longest_individual_lamp_run(self):
        reports = ReportStore(self.state, self.start, self.end, dry_run=True)
        reports.lamp_changed(self.start - timedelta(seconds=5), "kitchen")
        reports.lamp_changed(self.start + timedelta(seconds=60, milliseconds=100), "living")
        reports.lamp_changed(self.start + timedelta(seconds=119, milliseconds=600), None)
        reports.publish_due(self.end)
        final = self.read("trip.json")
        self.assertEqual(final["lamp_seconds"], 119)
        self.assertEqual(final["longest_lamp_run_seconds"], 61)

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

    def test_matching_id_without_report_data_is_archived_and_regenerated(self):
        reports = self.store()
        report = self.state / "daily" / "2030-06-11.json"
        expected_id = f"{reports.trip_id}:2030-06-11"
        original = json.dumps({"id": expected_id}).encode()
        report.write_bytes(original)
        with self.assertLogs("vacation_ghost.reports", "WARNING"):
            reports.publish_due(self.end)
        self.assertEqual(self.read("daily/2030-06-11.json")["id"], expected_id)
        self.assertIsInstance(self.read("daily/2030-06-11.json")["message"], str)
        self.assertEqual(next(report.parent.glob(f"{report.name}.corrupt-*")).read_bytes(), original)

    def test_existing_malformed_trip_owner_and_journal_are_preserved(self):
        owner = self.state / ".trip.json"
        owner.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(InvalidState, "missing or invalid trip identity"):
            self.store()
        self.assertEqual(owner.read_text(encoding="utf-8"), "{}")
        owner.unlink()
        reports = self.store()
        journal = self.state / ".activity.json"
        invalid = ({"trip_id": reports.trip_id}, {"trip_id": "different", "intervals": [], "open": None})
        for record in invalid:
            with self.subTest(record=record):
                original = json.dumps(record)
                journal.write_text(original, encoding="utf-8")
                with self.assertRaisesRegex(InvalidState, "Cannot use activity journal.*recover"):
                    self.store()
                self.assertEqual(journal.read_text(encoding="utf-8"), original)

    def test_legacy_journal_and_nightly_schedule_note_survive_restart(self):
        reports = self.store()
        evening = self.start + timedelta(hours=9)
        journal = self.state / ".activity.json"
        journal.write_text(json.dumps({
            "trip_id": reports.trip_id,
            "intervals": [[evening.isoformat(), (evening + timedelta(minutes=2)).isoformat()]],
            "open": None,
        }), encoding="utf-8")
        reports = self.store()
        note = "The evening routine was shortened because sunset was late."
        reports.record_schedule_note(evening.date(), note)
        self.store().publish_due(self.start + timedelta(days=1))
        daily = self.read("daily/2030-06-11.json")
        self.assertEqual(daily["lamp_seconds"], 120)
        self.assertEqual(daily["schedule_note"], note)
        self.assertIn(note, daily["message"])

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

    def test_new_final_report_fields_are_validated_only_when_present(self):
        reports = self.store()
        reports.complete(self.end)
        reports.publish_due(self.end)
        path = self.state / "trip.json"
        original = self.read("trip.json")
        old = original.copy()
        old.pop("relay_off_verified")
        old.pop("led_black_frame_sent")
        path.write_text(json.dumps(old))
        self.store().publish_due(self.end)
        self.assertEqual(self.read("trip.json"), old)
        for field, invalid in (("relay_off_verified", 17), ("led_black_frame_sent", "yes"),
                               ("longest_lamp_run_seconds", "30"), ("longest_lamp_run_seconds", True)):
            with self.subTest(field=field, invalid=invalid):
                broken = {**original, field: invalid}
                path.write_text(json.dumps(broken))
                with self.assertLogs("vacation_ghost.reports", "WARNING"):
                    self.store().publish_due(self.end)
                self.assertEqual(self.read("trip.json"), original)
                self.assertIn(broken, [json.loads(p.read_text()) for p in self.state.glob("trip.json.corrupt-*")])

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
