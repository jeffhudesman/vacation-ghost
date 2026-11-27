import json
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from tools.bench_record import main, render


class BenchRecordTest(unittest.TestCase):
    def test_record_starts_untested_and_only_exports_selected_fields(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            data = {"dry_run": False, "trip_id": "private-trip-id", "device_faults": 0,
                    "message": "private room name", "relay_off_verified": True}
            (directory / "latest.json").write_text(json.dumps(data))
            rehearsal = directory / "rehearsal.json"
            rehearsal.write_text(json.dumps({"outcome": "interrupted", "lamps_confirmed_on": ["private room"]}))
            record = directory / "bench.md"
            with redirect_stdout(StringIO()):
                main([str(record), "--state-dir", str(directory), "--rehearsal", str(rehearsal)])
            text = record.read_text()
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            self.assertIn("Untested", text)
            self.assertIn("relay_off_verified=true", text)
            self.assertIn("interrupted, 1 distinct lamp confirmed", text)
            self.assertNotIn("private", text)
            for change in ({"trip_id": "another"}, {"dry_run": True}, {"relay_off_verified": "yes"}):
                (directory / "trip.json").write_text(json.dumps(data | change))
                with self.assertRaises(ValueError):
                    render(directory)
