import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vacation_ghost.state import hardware_directory, read_record


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
                self.assertRaisesRegex(ValueError, "privileged state requires"),
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
