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
from .config import DEFAULT_LED_BACKEND, Config, load_config, parse_timestamp
from .engine import Controller, recover_outputs, rehearse, run, simulate
from .outputs import PiOutputs, SimulationOutputs
from .reports import ReportStore, read_trip_id, trip_identity


def _outputs(config: Config) -> PiOutputs:
    hardware = config.hardware
    if hardware is None:
        raise ValueError("this command requires a [hardware] section in the local configuration")
    return PiOutputs(
        hardware.plugs, hardware.led_count, hardware.led_pin, hardware.sounds_dir, hardware.volume,
        led_backend=hardware.led_backend,
    )


def _check_plug_identity(outputs: PiOutputs, room: str, checked_plugs: dict[str, str]) -> str:
    identity = outputs.plug_identity(room)
    other = checked_plugs.get(identity)
    if other is not None:
        raise RuntimeError(f"same Shelly device as {other}. Configure two distinct plugs.")
    checked_plugs[identity] = room
    return identity


def _doctor(config: Config) -> int:
    outputs = _outputs(config)
    hardware = config.hardware
    checked_plugs: dict[str, str] = {}

    def check_plug(room: str) -> str:
        _check_plug_identity(outputs, room, checked_plugs)
        return "on" if outputs.lamp_status(room) else "off"

    def check_glow() -> str:
        if not outputs.check_glow():
            return "disabled"
        return f"driver available ({hardware.led_backend}, BCM GPIO{hardware.led_pin}, {hardware.led_count} pixels)"

    def check_sound() -> str:
        if not outputs.check_audio():
            return "disabled"
        return f"player and cues available (volume {hardware.volume:g})"

    checks = {
        **{f"Plug {room}": lambda room=room: check_plug(room) for room in config.rooms},
        "TV glow": check_glow,
        "Sound": check_sound,
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
        owner = read_trip_id(path.parent)
        if owner is not None and owner != identity:
            raise ValueError("state directory belongs to a different trip")
        if _disarm_time(path, identity) is None:
            state.write_record(path, {"trip_id": identity, "requested_at": when.isoformat()})


def _disarm_time(path: Path, identity: str) -> datetime | None:
    request = state.read_record(path)
    if request is None:
        return None
    owner = request.get("trip_id")
    if not isinstance(owner, str) or not owner:
        raise state.InvalidState(f"invalid disarm request in {path}: expected a trip ID")
    if owner != identity:
        raise ValueError("disarm request belongs to a different trip; choose a fresh state directory")
    try:
        return parse_timestamp(request.get("requested_at"))
    except ValueError as exc:
        raise state.InvalidState(
            f"invalid disarm request in {path}: requested_at must be an ISO 8601 timestamp with a UTC offset"
        ) from exc


def _stop_event() -> Event:
    stop = Event()

    def request_stop(_signal, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return stop


def _refresh_recovered_status(config: Config, state_dir: Path, reservation: state.OutputReservation) -> None:
    """Publish confirmed physical shutdown when the existing reports are usable."""
    try:
        trip_id = read_trip_id(state_dir)
        if trip_id is None:
            reservation.clear_shutdown_confirmation()
            return
        dry_run = trip_id == trip_identity(config.start, config.end, dry_run=True)
        if not dry_run and trip_id != trip_identity(config.start, config.end):
            raise state.InvalidState(f"state directory belongs to a different trip: {state_dir}")
        reports = ReportStore(state_dir, config.start, config.end, dry_run=dry_run)
        now = datetime.now(config.timezone)
        if not dry_run and (request := _disarm_time(state_dir / ".disarm-request.json", trip_id)) is not None:
            reports.disarm(request)
        reports.confirm_shutdown(relay_off_verified=True, led_black_frame_sent=reservation.led_black_frame_sent)
        reports.status(now, False, None, force=True)
        if not dry_run:
            reports.publish_due(now)
        reservation.clear_shutdown_confirmation()
    except Exception as exc:
        logging.warning("Could not refresh the existing reports after shutdown. Retry recover after fixing the issue: %s", exc)


def _recover_pending_outputs(config: Config, outputs: PiOutputs, reservation: state.OutputReservation) -> bool:
    if reservation.engaged and reservation.plug_identities is None and not reservation.shutdown_confirmed:
        logging.warning("Legacy shutdown record. Recovery uses the configured plug addresses without stored MACs.")
    return recover_outputs(config.rooms, outputs, reservation, led_enabled=bool(config.hardware.led_count))


def _shutdown_message(led_black_frame_sent: bool | None) -> str:
    message = "Shutdown confirmed. Relay off states verified."
    if led_black_frame_sent is True:
        return message + " LED blackout frame sent."
    if led_black_frame_sent is False:
        return message + " LED blackout frame delivery unconfirmed."
    return message


def _hardware_command(command: str, config: Config, state_dir: Path) -> None:
    outputs = _outputs(config)
    state.hardware_directory(state_dir)
    state.hardware_directory(state.DEVICE_DIR)
    is_rehearsal = command == "rehearse"
    disarm_path = state_dir / ".disarm-request.json"
    identity = trip_identity(config.start, config.end)

    def identify_plugs() -> dict[str, str]:
        checked_plugs: dict[str, str] = {}
        identities = {room: _check_plug_identity(outputs, room, checked_plugs) for room in config.rooms}
        outputs.bind_plug_identities(identities)
        return identities

    try:
        if command == "disarm":
            try:
                _request_disarm(disarm_path, identity, datetime.now(config.timezone))
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(
                    f"Could not record disarm ({exc}). Stop the runner and use recover with the original "
                    "configuration and state directory to retry any pending shutdown."
                ) from exc
        with state.lock_file(state.DEVICE_DIR / "hardware.lock"), state.lock_file(state_dir / ".lock"):
            hardware = config.hardware
            settings = [hardware.plugs, hardware.led_count, hardware.led_pin]
            # Preserve pending shutdown records created with the original backend.
            if hardware.led_count and hardware.led_backend != DEFAULT_LED_BACKEND:
                settings.append(hardware.led_backend)
            hardware_id = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
            reservation = state.OutputReservation(
                state_dir, state.DEVICE_DIR, hardware_id=hardware_id, identify_plugs=identify_plugs,
            )
            if reservation.plug_identities is not None:
                if set(reservation.plug_identities) != set(config.rooms):
                    raise state.InvalidState("pending shutdown record does not identify every configured room")
                outputs.bind_plug_identities(reservation.plug_identities)
            if command == "recover":
                if _recover_pending_outputs(config, outputs, reservation):
                    print(_shutdown_message(reservation.led_black_frame_sent))
                    _refresh_recovered_status(config, state_dir, reservation)
                else:
                    print("No pending shutdown for this state directory.")
                return
            if reservation.shutdown_confirmed:
                _recover_pending_outputs(config, outputs, reservation)
                _refresh_recovered_status(config, state_dir, reservation)
            try:
                reports = ReportStore(state_dir, config.start, config.end, dry_run=is_rehearsal)
            except Exception as report_error:
                if not reservation.engaged:
                    raise
                try:
                    _recover_pending_outputs(config, outputs, reservation)
                except (OSError, ValueError, RuntimeError) as shutdown_error:
                    raise RuntimeError(f"Could not open reports: {report_error}. {shutdown_error}") from shutdown_error
                raise RuntimeError(f"Shutdown confirmed. Could not open reports: {report_error}") from report_error
            if reservation.engaged and reservation.plug_identities is None:
                _recover_pending_outputs(config, outputs, reservation)
                _refresh_recovered_status(config, state_dir, reservation)
                reports = ReportStore(state_dir, config.start, config.end, dry_run=is_rehearsal)
            controller = Controller(config.rooms, outputs, reports)
            stop = _stop_event()
            if is_rehearsal:
                rehearse(config, controller, stop, reservation)
                print(f"Rehearsal ended. {_shutdown_message(reports.led_black_frame_sent)}")
            else:
                run(config, controller, stop, reservation=reservation,
                    disarm_requested=lambda: _disarm_time(disarm_path, identity))
                if command == "disarm":
                    print("Disarm recorded.")
                    if reports.relay_off_verified:
                        print(_shutdown_message(reports.led_black_frame_sent))
    except state.DeviceBusy:
        if command != "disarm":
            raise
        print("Disarm request saved for this trip. Check latest.json for shutdown status.")
    finally:
        outputs.close()


def _at_timestamp(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use an ISO 8601 date and time with a UTC offset, for example 2030-06-11T09:00:00Z"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a private, local occupancy routine.")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check configuration and hardware without changing outputs")
    for name, help_text in (
        ("run", "control the devices on a Raspberry Pi"),
        ("rehearse", "exercise each enabled output and confirm shutdown"),
        ("disarm", "end this trip early, including across reboot"),
        ("recover", "retry a pending shutdown without resuming the routine"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--state-dir", required=True, type=Path)
    sim = commands.add_parser("simulate", help="replay the trip without devices")
    sim.add_argument("--state-dir", required=True, type=Path, help="an empty directory for simulated reports")
    sim.add_argument("--at", type=_at_timestamp, help="replay through this offset timestamp, defaulting to trip end")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = load_config(
            args.config, validate_sunset=args.command not in {"recover", "disarm"},
            validate_settings=args.command == "doctor",
        )
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
