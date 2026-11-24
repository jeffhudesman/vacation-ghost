"""Command-line interface for local development and Raspberry Pi operation."""

import argparse
import hashlib
import json
import logging
import signal
from datetime import datetime
from pathlib import Path
from threading import Event

from . import state
from .config import Config, load_config, parse_timestamp
from .engine import Controller, rehearse, run, simulate
from .outputs import PiOutputs, SimulationOutputs
from .reports import ReportStore, trip_identity


def _outputs(config: Config) -> PiOutputs:
    hardware = config.hardware
    if hardware is None:
        raise ValueError("this command requires a [hardware] section in the local configuration")
    return PiOutputs(hardware.plugs, hardware.led_count, hardware.led_pin, hardware.sounds_dir, hardware.volume)


def _doctor(config: Config) -> int:
    outputs = _outputs(config)
    checks = {
        **{f"Plug {room}": lambda room=room: "on" if outputs.lamp_status(room) else "off" for room in config.rooms},
        "TV glow": lambda: "driver available" if outputs.check_glow() else "disabled",
        "Sound": lambda: "player and cues available" if outputs.check_audio() else "disabled",
    }
    failed = False
    for label, check in checks.items():
        try:
            print(f"{label}: {check()}")
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"{label}: failed ({exc})")
            failed = True
    return int(failed)


def _request_disarm(path: Path, identity: str, when: datetime) -> None:
    with state.lock_file(path.parent / ".disarm.lock", blocking=True):
        owner = state.read_record(path.parent / ".trip.json").get("trip_id")
        if owner is not None and owner != identity:
            raise ValueError("state directory belongs to a different trip")
        if _disarm_time(path, identity) is None:
            state.write_record(path, {"trip_id": identity, "requested_at": when.isoformat()})


def _disarm_time(path: Path, identity: str) -> datetime | None:
    request = state.read_record(path)
    if not request:
        return None
    if request.get("trip_id") != identity:
        raise ValueError("disarm request belongs to a different trip; choose a fresh state directory")
    return parse_timestamp(request["requested_at"])


def _stop_event() -> Event:
    stop = Event()

    def request_stop(_signal, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return stop


def _hardware_command(command: str, config: Config, state_dir: Path) -> None:
    outputs = _outputs(config)
    state.private_directory(state_dir)
    state.private_directory(state.DEVICE_DIR)
    is_rehearsal = command == "rehearse"
    disarm_path = state_dir / ".disarm-request.json"
    identity = trip_identity(config.start, config.end)
    if command == "disarm":
        _request_disarm(disarm_path, identity, datetime.now(config.timezone))
    try:
        with state.lock_file(state.DEVICE_DIR / "hardware.lock"), state.lock_file(state_dir / ".lock"):
            hardware = config.hardware
            settings = json.dumps([hardware.plugs, hardware.led_count, hardware.led_pin], sort_keys=True)
            hardware_id = hashlib.sha256(settings.encode()).hexdigest()
            reservation = state.OutputReservation(state_dir, state.DEVICE_DIR, hardware_id=hardware_id)
            reports = ReportStore(state_dir, config.start, config.end, dry_run=is_rehearsal)
            controller = Controller(config.rooms, outputs, reports)
            stop = _stop_event()
            if is_rehearsal:
                rehearse(config, controller, stop, reservation)
                print("Rehearsal ended and shutdown was confirmed.")
            else:
                run(config, controller, stop, reservation=reservation,
                    disarm_requested=lambda: _disarm_time(disarm_path, identity))
                if command == "disarm":
                    print("Disarm recorded. Shutdown complete.")
    except state.DeviceBusy:
        if command != "disarm":
            raise
        print("Disarm request saved for this trip. Check latest.json for shutdown status.")
    finally:
        outputs.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a private, local occupancy routine.")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check configuration and hardware without changing outputs")
    for name, help_text in (
        ("run", "control the devices on a Raspberry Pi"),
        ("rehearse", "exercise each enabled output and confirm shutdown"),
        ("disarm", "end this trip early, including across reboot"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--state-dir", required=True, type=Path)
    sim = commands.add_parser("simulate", help="replay the trip without devices")
    sim.add_argument("--state-dir", required=True, type=Path, help="an empty directory for simulated reports")
    sim.add_argument("--at", type=parse_timestamp, help="replay through this offset timestamp, defaulting to trip end")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "simulate":
            state.private_directory(args.state_dir)
            lock_path = args.state_dir / ".lock"
            with state.lock_file(lock_path):
                if any(path != lock_path for path in args.state_dir.iterdir()):
                    raise ValueError("simulate requires an empty state directory")
                reports = ReportStore(args.state_dir, config.start, config.end, dry_run=True)
                simulate(config, SimulationOutputs(), reports, args.at or config.end)
            print(f"Simulated reports written to {args.state_dir}")
        else:
            _hardware_command(args.command, config, args.state_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Vacation Ghost: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
