import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from vacation_ghost import __main__ as cli
from vacation_ghost.config import load_config


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "vacation.lights-only.toml"


class ConfigSettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config_path = Path(directory.name) / "trip.toml"
        self.state_dir = Path(directory.name) / "state"
        self.config_text = EXAMPLE.read_text()

    def test_strict_settings_identify_misspellings_and_allow_room_names(self):
        config_text = self.config_text.replace("kitchen", "den").replace("living", "office")
        self.config_path.write_text(config_text)
        self.assertEqual(load_config(self.config_path, validate_settings=True).rooms, ("den", "office"))

        config_text = config_text.replace("[trip]", '[trip]\nznoe = "UTC"')
        config_text = config_text.replace("[routine]", '[routine]\nsead = "example"')
        config_text = config_text.replace("led_count = 0", 'led_count = 20\nled_backed = "spi"')
        config_text = config_text.replace("volume = 0", 'volum = 0\nsounds_dir = "cues"')
        self.config_path.write_text(config_text + "\n[notes]\n")

        with self.assertRaisesRegex(ValueError, "Unrecognized configuration settings") as error:
            load_config(self.config_path, validate_settings=True)
        message = str(error.exception)
        for key in ("trip.znoe", "routine.sead", "hardware.led_backed", "hardware.volum", "notes"):
            self.assertIn(key, message)

        hardware = load_config(self.config_path).hardware
        self.assertEqual((hardware.led_backend, hardware.led_pin, hardware.volume), ("rpi_ws281x", 18, 0.08))

    def test_doctor_reports_unknown_settings_before_checking_hardware(self):
        self.config_path.write_text(self.config_text.replace("volume = 0", "volum = 0"))
        with (
            patch.object(cli, "_doctor") as doctor,
            redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as exit_code,
        ):
            cli.main(["--config", str(self.config_path), "doctor"])
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn("hardware.volum", error.getvalue())
        doctor.assert_not_called()

    def test_shutdown_commands_can_load_configs_with_older_unsupported_fields(self):
        self.config_path.write_text(self.config_text.replace("volume = 0", "volume = 0\nlegacy_field = false"))
        for command in ("disarm", "recover"):
            with self.subTest(command=command), patch.object(cli, "_hardware_command") as hardware_command:
                code = cli.main(["--config", str(self.config_path), command, "--state-dir", str(self.state_dir)])
                self.assertEqual(code, 0)
                hardware_command.assert_called_once_with(command, load_config(self.config_path), self.state_dir)
