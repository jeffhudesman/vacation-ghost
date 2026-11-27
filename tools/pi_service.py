"""Commission, disarm, or archive an installed Vacation Ghost Pi service."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


APP = Path("/opt/vacation-ghost")
CONFIG = Path("/etc/vacation-ghost/vacation.toml")
STATE = Path("/var/lib/vacation-ghost")
UNIT = "vacation-ghost.service"
SCRIPT = Path(__file__).resolve()


def execute(*args, capture=False):
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def service_properties():
    fields = ("User", "ActiveState", "NRestarts", "InvocationID")
    result = execute("sudo", "systemctl", "show", UNIT, "--property=" + ",".join(fields), capture=True)
    return dict(line.split("=", 1) for line in result.splitlines())


def account_command(user, *args, capture=False):
    return execute("sudo", "-u", user, "-g", user, APP / ".venv/bin/python", *args, capture=capture)


def inspect_as(user, command, *args):
    return account_command(user, "-I", SCRIPT, command, *args, capture=True)


def commission(user):
    unit_file = Path("/etc/systemd/system") / UNIT
    execute("sudo", "install", "-o", "root", "-g", "root", "-m", "0644", APP / "deploy" / UNIT, unit_file)
    execute("sudo", "systemd-analyze", "verify", unit_file)
    execute("sudo", "systemctl", "daemon-reload")
    initial = service_properties()
    if (initial["User"] or "root") != user:
        raise ValueError("Installed service account differs from --service-user. Check the service profile.")
    if initial["ActiveState"] not in ("inactive", "failed"):
        raise ValueError("Stop and confirm shutdown of the existing service before commissioning.")
    if initial["ActiveState"] == "failed" or initial["NRestarts"] != "0":
        execute("sudo", "systemctl", "reset-failed", UNIT)
        initial = service_properties()
    trip, inode = inspect_as(user, "_before").split()
    since = str(time.time())
    execute("sudo", "systemctl", "start", UNIT)
    invocation = service_properties()["InvocationID"]
    if not re.fullmatch(r"[0-9a-f]{32}", invocation) or invocation in ("0" * 32, initial["InvocationID"]):
        raise ValueError("A new systemd invocation could not be confirmed.")
    updated = inspect_as(user, "_idle", trip, inode, since)
    current = service_properties()
    if (current["InvocationID"] != invocation or current["NRestarts"] != "0"
            or current["ActiveState"] != "active" or (current["User"] or "root") != user):
        raise ValueError("Service exited, changed profile, or restarted during commissioning.")
    execute("sudo", "systemctl", "enable", UNIT)
    print(f"Vacation Ghost is idle and waiting for departure ({updated}). Startup at boot is enabled.")


def terminal_status(current, device, trip):
    from vacation_ghost import state

    if any(os.path.lexists(path) for path in (
        device / "outputs.json", current / ".outputs-active", current / ".shutdown-confirmed.json",
    )):
        return False
    status = state.read_record(current / "latest.json") or {}
    owner = state.read_record(current / ".trip.json") or {}
    if (owner.get("trip_id") != trip or status.get("trip_id") != trip or status.get("dry_run") is not False
            or status.get("active") is not False or status.get("shutdown_unconfirmed") is not False
            or status.get("lamp_room", "unknown") is not None or status.get("relay_off_verified") is False):
        return False
    if status.get("disarmed") is True and status.get("completed") is False:
        marker = state.read_record(current / ".disarm-request.json") or {}
        journal = state.read_record(current / ".activity.json") or {}
        if journal.get("trip_id") != trip or not journal.get("disarmed_at"):
            return False
        terminal_at = marker.get("requested_at")
    elif status.get("completed") is True and status.get("disarmed") is False:
        marker = state.read_record(current / ".completion.json") or {}
        terminal_at = marker.get("completed_at")
    else:
        return False
    if marker.get("trip_id") != trip:
        return False
    observed = datetime.fromisoformat(status["updated_at"])
    terminal = datetime.fromisoformat(terminal_at)
    return observed.utcoffset() is not None and terminal.utcoffset() is not None and observed >= terminal


def finish_trip(current, device, trip, *, archive=False, timeout=120):
    from vacation_ghost import state

    deadline = time.monotonic() + timeout
    while True:
        try:
            with state.lock_file(device / "hardware.lock"):
                if archive and not os.path.lexists(current):
                    if os.path.lexists(device / "outputs.json"):
                        raise ValueError("A device reservation is pending. Recover it with its original state.")
                    return "There is no current trip to archive."
                state.hardware_directory(current)
                with state.lock_file(current / ".lock"), state.lock_file(current / ".disarm.lock"):
                    if terminal_status(current, device, trip):
                        if not archive:
                            return "The configured trip has ended. No output shutdown is pending."
                        backup = Path(tempfile.mkdtemp(prefix="trip-previous.", dir=current.parent))
                        try:
                            current.rename(backup / "current")
                        except OSError:
                            backup.rmdir()
                            raise
                        state.sync_directory(backup)
                        state.sync_directory(current.parent)
                        return f"Previous trip saved in {backup / 'current'}"
        except state.DeviceBusy:
            pass
        if archive or time.monotonic() >= deadline:
            raise ValueError("Terminal status and released outputs could not be confirmed. Keep the original "
                             "config and current directory. Inspect the service and run recover for a pending shutdown.")
        time.sleep(0.5)


def inspect_install(command, args):
    # Load only the installed runtime, including its release-specific identity and locking rules.
    sys.path.insert(0, str(APP))
    from vacation_ghost import state
    from vacation_ghost.config import load_config
    from vacation_ghost.reports import trip_identity

    current = STATE / "current"
    device = STATE / "device"
    config = load_config(CONFIG, validate_sunset=command in ("_before", "_idle"))
    trip = trip_identity(config.start, config.end)
    path = current / "latest.json"
    if command == "_before":
        if config.start.astimezone(UTC) <= datetime.now(UTC):
            raise ValueError("Set a future departure before starting the service.")
        inode = path.lstat().st_ino if os.path.lexists(path) else 0
        return f"{trip} {inode}"
    if command == "_idle":
        expected, previous_inode, since = args
        if expected != trip:
            raise ValueError("Trip changed during commissioning.")
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                with open(path, encoding="utf-8", opener=state._private_opener) as stream:
                    inode = os.fstat(stream.fileno()).st_ino
                    status = json.load(stream)
                updated = datetime.fromisoformat(status["updated_at"])
                if inode != int(previous_inode) and updated.utcoffset() is not None and updated.timestamp() >= float(since):
                    healthy = (status.get("trip_id") == trip and status.get("dry_run") is False
                               and status.get("lamp_room", "unknown") is None
                               and all(status.get(key) is False for key in (
                                   "active", "disarmed", "completed", "shutdown_unconfirmed"))
                               and type(status.get("device_faults")) is int and status["device_faults"] == 0
                               and datetime.now(UTC) < config.start.astimezone(UTC))
                    if not healthy:
                        raise RuntimeError("Fresh status did not show this trip idle and healthy before departure.")
                    return updated.isoformat()
            except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
            time.sleep(0.5)
        raise ValueError("No fresh idle status was written within 35 seconds.")
    state.hardware_directory(device)
    return finish_trip(current, device, trip, archive=command == "_archive")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("commission", "disarm", "archive",
                                           "_before", "_idle", "_terminal", "_archive"), metavar="commission|disarm|archive")
    parser.add_argument("arguments", nargs="*", metavar="ARG")
    parser.add_argument("--service-user", choices=("vacation-ghost", "root"), default="vacation-ghost")
    args = parser.parse_args(argv)
    if len(args.arguments) != (3 if args.command == "_idle" else 0):
        parser.error("unexpected arguments for " + args.command)
    try:
        if args.command.startswith("_"):
            print(inspect_install(args.command, args.arguments))
        elif args.command == "commission":
            commission(args.service_user)
        else:
            user = service_properties()["User"] or "root"
            if user != args.service_user:
                raise ValueError("Installed service account differs from --service-user. Check the service profile.")
            if args.command == "disarm":
                os.chdir(APP)
                account_command(user, "-Es", "-m", "vacation_ghost", "--config", CONFIG, "disarm",
                                "--state-dir", STATE / "current")
                print(inspect_as(user, "_terminal"))
            else:
                execute("sudo", "systemctl", "disable", "--now", UNIT)
                print(inspect_as(user, "_archive"))
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Vacation Ghost: {exc}\n")


if __name__ == "__main__":
    main()
