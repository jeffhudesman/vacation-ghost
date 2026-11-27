import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from tools import pi_service
from vacation_ghost import state
from vacation_ghost.config import load_config
from vacation_ghost.reports import trip_identity


class PiServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.current = self.root / "current"
        self.device = self.root / "device"
        self.current.mkdir()
        self.device.mkdir()
        self.config = self.root / "vacation.toml"
        now = datetime.now(UTC)
        self.now = now.isoformat()
        self.config.write_text(
            f'[trip]\nstart = "{(now + timedelta(days=1)).isoformat()}"\n'
            f'end = "{(now + timedelta(days=2)).isoformat()}"\ntimezone = "UTC"\n'
            '[routine]\nrooms = ["kitchen", "living"]\nsunset = "19:00"\nbedtime = "21:00"\n'
        )
        config = load_config(self.config)
        self.trip = trip_identity(config.start, config.end)
        self.status = {
            "trip_id": self.trip, "updated_at": self.now, "active": False, "disarmed": False,
            "completed": False, "dry_run": False, "lamp_room": None, "shutdown_unconfirmed": False, "device_faults": 0,
        }
        self.save(".trip.json", {"trip_id": self.trip})
        self.save("latest.json", self.status)
        self.enterContext(patch.object(pi_service, "STATE", self.root))
        self.enterContext(patch.object(pi_service, "CONFIG", self.config))
        # The real hardware directory policy rejects the shared ancestor /tmp.
        self.enterContext(patch.object(state, "hardware_directory"))

    def save(self, name, data):
        state.write_record(self.current / name, data)

    def finish(self, *, archive=False):
        return pi_service.finish_trip(self.current, self.device, self.trip, archive=archive, timeout=0)

    def disarmed(self):
        self.status["disarmed"] = True
        self.save("latest.json", self.status)
        self.save(".disarm-request.json", {"trip_id": self.trip, "requested_at": self.now})
        self.save(".activity.json", {"trip_id": self.trip, "disarmed_at": self.now})

    def test_idle_and_queued_disarm_are_not_a_terminal_shutdown(self):
        self.save(".disarm-request.json", {"trip_id": self.trip, "requested_at": self.now})
        with self.assertRaisesRegex(ValueError, "could not be confirmed"):
            self.finish(archive=True)
        self.assertTrue(self.current.is_dir())

    def test_terminal_requires_released_outputs_and_fresh_matching_status(self):
        self.disarmed()
        for marker in [self.device / "outputs.json", self.current / ".outputs-active",
                       self.current / ".shutdown-confirmed.json"]:
            with self.subTest(marker=marker.name):
                marker.touch()
                with self.assertRaisesRegex(ValueError, "could not be confirmed"):
                    self.finish(archive=True)
                marker.unlink()
        with state.lock_file(self.device / "hardware.lock"), self.assertRaisesRegex(ValueError, "could not be confirmed"):
            self.finish()
        for change in ({"trip_id": "other"}, {"active": True}, {"dry_run": True}, {"relay_off_verified": False},
                       {"shutdown_unconfirmed": True}, {"updated_at": "2000-01-01T00:00:00+00:00"}):
            with self.subTest(change=change):
                self.save("latest.json", {**self.status, **change})
                with self.assertRaisesRegex(ValueError, "could not be confirmed"):
                    self.finish(archive=True)
        self.assertTrue(self.current.is_dir())

    def test_never_engaged_disarm_can_be_archived_without_relay_readback(self):
        self.disarmed()
        self.assertIn("No output shutdown is pending", self.finish())
        self.assertIn("Previous trip saved", self.finish(archive=True))
        self.assertFalse(self.current.exists())
        archived, = self.root.glob("trip-previous.*/current")
        self.assertEqual(json.loads((archived / "latest.json").read_text())["trip_id"], self.trip)
        self.assertTrue(self.device.is_dir())

    def test_completed_trip_and_missing_current(self):
        self.save("latest.json", {**self.status, "completed": True})
        self.save(".completion.json", {"trip_id": self.trip, "completed_at": self.now})
        self.finish(archive=True)
        self.assertIn("no current trip", self.finish(archive=True))
        (self.device / "outputs.json").touch()
        with self.assertRaisesRegex(ValueError, "pending"):
            self.finish(archive=True)

    def test_idle_requires_new_inode_fresh_timestamp_and_matching_healthy_trip(self):
        trip, inode = pi_service.inspect_install("_before", []).split()
        self.assertEqual(trip, self.trip)
        with patch.object(pi_service.time, "monotonic", side_effect=[0, 40]):
            with self.assertRaisesRegex(ValueError, "fresh idle status"):
                pi_service.inspect_install("_idle", [trip, inode, "0"])
        self.save("latest.json", self.status)
        self.assertEqual(pi_service.inspect_install("_idle", [trip, inode, "0"]), self.now)
        for change in ({"trip_id": "different"}, {"device_faults": 1}, {"disarmed": True}, {"dry_run": True}):
            with self.subTest(change=change):
                self.save("latest.json", {**self.status, **change})
                with self.assertRaisesRegex(RuntimeError, "idle and healthy"):
                    pi_service.inspect_install("_idle", [trip, "0", "0"])

    def test_commission_declines_a_live_unit_before_modifying_anything(self):
        for active in ("active", "activating", "deactivating", "reloading"):
            with self.subTest(active=active), patch.object(pi_service, "execute") as commands, \
                    patch.object(pi_service, "service_properties", return_value={"ActiveState": active}):
                with self.assertRaisesRegex(ValueError, "Stop and confirm shutdown"):
                    pi_service.commission("vacation-ghost")
                commands.assert_not_called()

    def test_unit_lookup_allows_only_explicit_inactive_not_found_on_first_install(self):
        missing = "LoadState=not-found\nActiveState=inactive\n"
        for code in (0, 4):
            result = subprocess.CompletedProcess([], code, stdout=missing)
            with self.subTest(code=code), patch.object(pi_service.subprocess, "run", return_value=result):
                self.assertEqual(pi_service.service_properties(allow_missing=True)["LoadState"], "not-found")
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    pi_service.service_properties()
        denied = subprocess.CompletedProcess([], 1, stdout="")
        with patch.object(pi_service.subprocess, "run", return_value=denied), self.assertRaises(subprocess.CalledProcessError):
            pi_service.service_properties(allow_missing=True)

    def test_commission_enables_only_healthy_new_service_invocation(self):
        initial = {"User": "vacation-ghost", "ActiveState": "inactive", "InvocationID": "", "NRestarts": "0"}
        running = {**initial, "ActiveState": "active", "InvocationID": "1" * 32}
        for change in (None, {"NRestarts": "1"}, {"InvocationID": "2" * 32}, {"ActiveState": "failed"}):
            with self.subTest(change=change), patch.object(pi_service, "execute") as execute, \
                    patch.object(pi_service, "service_properties", side_effect=[initial, initial, running, {**running, **(change or {})}]), \
                    patch.object(pi_service, "inspect_as", side_effect=[f"{self.trip} 0", self.now]), redirect_stdout(StringIO()):
                if change is None:
                    pi_service.commission("vacation-ghost")
                    self.assertEqual(execute.call_args, call("sudo", "systemctl", "enable", pi_service.UNIT))
                else:
                    with self.assertRaisesRegex(ValueError, "restarted"):
                        pi_service.commission("vacation-ghost")
                    self.assertNotIn(call("sudo", "systemctl", "enable", pi_service.UNIT), execute.call_args_list)

    def test_guide_pins_the_installed_helper_bytes(self):
        root = Path(__file__).resolve().parents[1]
        helper = (root / "tools/pi_service.py").read_bytes()
        guide = (root / "docs/service.md").read_text()
        self.assertRegex(guide, r"https://raw\.githubusercontent\.com/jeffhudesman/vacation-ghost/[0-9a-f]{40}/tools/pi_service\.py")
        self.assertIn(hashlib.sha256(helper).hexdigest(), guide)
