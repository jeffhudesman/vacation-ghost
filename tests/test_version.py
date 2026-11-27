import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vacation_ghost import version
from vacation_ghost.__main__ import main


class VersionTests(unittest.TestCase):
    def test_appliance_receipt_identifies_install_without_config_or_git(self):
        with tempfile.TemporaryDirectory() as name:
            package = Path(name)
            receipt = package / "BUILD.json"
            sha = "abcde01234" * 4
            receipt.write_text(json.dumps({"commit": sha, "version": "0.1.0a3.dev0"}))
            with patch.object(version, "__file__", str(package / "version.py")):
                output = StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as exit_:
                    main(["--version"])
                self.assertEqual(exit_.exception.code, 0)
                self.assertIn(sha, output.getvalue())
                receipt.write_text("invalid")
                self.assertIn("invalid installed", version.build_description())
