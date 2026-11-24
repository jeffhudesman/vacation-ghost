import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vacation_ghost.state import InvalidState, OutputReservation, hardware_directory, private_directory, read_record, write_record


class StateSecurityTests(unittest.TestCase):
    def test_privileged_state_rejects_untrusted_path_components(self):
        path = Path("/trusted/parent/trip")
        root_dir = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        invalid_parents = (
            SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0),
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o770, st_uid=0),
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000),
        )
        for invalid in invalid_parents:
            def info(component):
                return invalid if component == path.parent else root_dir

            with (
                self.subTest(invalid=invalid),
                patch("vacation_ghost.state.os.geteuid", return_value=0),
                patch.object(Path, "lstat", info),
                self.assertRaisesRegex(InvalidState, "hardware state requires trusted ownership"),
            ):
                hardware_directory(path)

    def test_record_rejects_symlink_and_fifo(self):
        with TemporaryDirectory() as root:
            state = Path(root)
            target = state / "target.json"
            target.write_text("{}")
            (state / "link.json").symlink_to(target)
            os.mkfifo(state / "pipe")
            with self.assertRaises(OSError):
                read_record(state / "link.json")
            with self.assertRaisesRegex(ValueError, "regular file"):
                read_record(state / "pipe")

    def test_existing_controller_paths_reject_bad_owner_or_write_permissions(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / "daily"
            directory.mkdir(mode=0o770)
            directory.chmod(0o770)
            with self.assertRaisesRegex(InvalidState, "without group or public writes"):
                private_directory(directory)
            directory.chmod(0o700)
            different_owner = os.geteuid() + 1
            with (
                patch("vacation_ghost.state.os.geteuid", return_value=different_owner),
                self.assertRaisesRegex(InvalidState, "owned by the controller"),
            ):
                private_directory(directory)

            file = directory / "latest.json"
            file.write_text("{}", encoding="utf-8")
            file.chmod(0o660)
            with self.assertRaisesRegex(InvalidState, "without group or public writes"):
                read_record(file)
            with self.assertRaisesRegex(InvalidState, "without group or public writes"):
                write_record(file, {"unexpected": "replacement"})
            self.assertEqual(file.read_text(encoding="utf-8"), "{}")
            file.chmod(0o600)
            with (
                patch("vacation_ghost.state.os.geteuid", return_value=different_owner),
                self.assertRaisesRegex(InvalidState, "owned by the controller"),
            ):
                read_record(file)

    def test_missing_record_is_distinct_from_an_empty_or_invalid_record(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "record.json"
            self.assertIsNone(read_record(path))
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(read_record(path), {})
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(InvalidState, "must contain a JSON object"):
                read_record(path)

    def test_modern_reservation_rejects_partial_metadata_and_keeps_legacy_separate(self):
        with TemporaryDirectory() as root:
            state = Path(root) / "trip"
            device = Path(root) / "device"
            state.mkdir()
            device.mkdir()
            path = device / "outputs.json"
            for details in (
                '"glow_pending": false',
                '"hardware_id": "", "glow_pending": false',
                '"hardware_id": "original", "glow_pending": 0',
            ):
                with self.subTest(details=details):
                    original = f'{{"state_dir": "{state}", {details}}}'
                    path.write_text(original, encoding="utf-8")
                    with self.assertRaisesRegex(InvalidState, "off manually.*preserve.*hardware metadata"):
                        OutputReservation(state, device, hardware_id="original")
                    self.assertEqual(path.read_text(encoding="utf-8"), original)

            path.unlink()
            (state / ".outputs-active").touch(mode=0o600)
            legacy = OutputReservation(state, device, hardware_id="original")
            self.assertTrue(legacy.engaged)
            self.assertTrue(legacy.glow_pending)
