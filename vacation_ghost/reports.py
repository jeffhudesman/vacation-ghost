"""Local activity summaries and a restart-safe record of lamp activity."""

import hashlib
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from .state import _private_opener
from .state import private_directory as _private_directory
from .state import read_record as _read_json
from .state import sync_directory
from .state import write_record as _write_json


logger = logging.getLogger(__name__)


@dataclass
class _LampRun:
    room: str
    start: datetime
    observed_through: datetime


@dataclass(frozen=True)
class _DeviceFault:
    at: datetime
    device: str
    shutdown_unconfirmed: bool


def _utc(when: datetime) -> datetime:
    if when.utcoffset() is None:
        raise ValueError("Report timestamps must include a timezone")
    return when.astimezone(UTC)


def trip_identity(trip_start: datetime, trip_end: datetime, *, dry_run: bool = False) -> str:
    """Identify the trip and mode associated with a state directory."""
    identity = f"{_utc(trip_start).isoformat()}|{_utc(trip_end).isoformat()}|{trip_start.tzinfo}|{dry_run}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _clock_time(when: datetime) -> str:
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def _duration(seconds: int) -> str:
    if seconds < 60:
        return "under a minute"
    hours, minutes = divmod(seconds // 60, 60)
    if not hours:
        return f"{minutes} min"
    if not minutes:
        return f"{hours} hr"
    return f"{hours} hr {minutes} min"


def _fault_summary(faults: list[_DeviceFault], shutdown_unconfirmed: bool) -> dict:
    counts = Counter(fault.device for fault in faults)
    return {
        "device_faults": len(faults),
        "faults_by_device": dict(sorted(counts.items())),
        "shutdown_unconfirmed": shutdown_unconfirmed,
    }


def _fault_message(faults: dict) -> str:
    message = ""
    if faults["device_faults"]:
        devices = ", ".join(faults["faults_by_device"])
        message = f" Device controls reported faults ({devices})."
    if faults["shutdown_unconfirmed"]:
        message += " Output shutdown could not be confirmed."
    return message


def _preserve_malformed_report(path: Path) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex}")
    os.link(path, backup, follow_symlinks=False)
    sync_directory(path.parent)
    logger.warning("Preserved malformed report %s in %s and will regenerate it", path, backup)


def _published_report_id(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", opener=_private_opener) as stream:
            report = json.load(stream)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        report = None

    if isinstance(report, dict) and isinstance(report.get("id"), str):
        return report["id"]
    _preserve_malformed_report(path)
    return None


class ReportStore:
    """Record confirmed lamp transitions and publish summaries inside a state directory."""

    def __init__(
        self, state_dir: Path, trip_start: datetime, trip_end: datetime, *, dry_run: bool = False
    ):
        self._start = _utc(trip_start)
        self._end = _utc(trip_end)
        if self._end <= self._start:
            raise ValueError("Trip must end after it starts")
        self._zone = trip_start.tzinfo
        self._dry_run = dry_run
        self.trip_id = trip_identity(trip_start, trip_end, dry_run=dry_run)
        self._prefix = "Simulation. " if dry_run else ""
        self.state_dir = Path(state_dir)
        _private_directory(self.state_dir)
        owner_path = self.state_dir / ".trip.json"
        owner = _read_json(owner_path).get("trip_id")
        if owner is None:
            _write_json(owner_path, {"trip_id": self.trip_id})
        elif owner != self.trip_id:
            raise ValueError("state directory belongs to a different trip; choose a fresh directory")
        self._daily = self.state_dir / "daily"
        _private_directory(self._daily)
        self._journal = self.state_dir / ".activity.json"
        self._open: _LampRun | None = None
        self._intervals: list[tuple[datetime, datetime]] = []
        self._faults: list[_DeviceFault] = []
        self._shutdown_unconfirmed = False
        self._disarmed_at: datetime | None = None
        self._last_status: tuple[bool, str | None, bool, int, bool] | None = None
        self._next_night = trip_start.date()
        self._trip_published = False
        self._restore_journal()

    def _save_journal(self) -> None:
        open_run = None
        if self._open is not None:
            open_run = {
                "start": self._open.start.isoformat(),
                "observed_through": self._open.observed_through.isoformat(),
            }
        _write_json(self._journal, {
            "trip_id": self.trip_id,
            "intervals": [[start.isoformat(), end.isoformat()] for start, end in self._intervals],
            "open": open_run,
            "faults": [
                [fault.at.isoformat(), fault.device, fault.shutdown_unconfirmed] for fault in self._faults
            ],
            "shutdown_unconfirmed": self._shutdown_unconfirmed,
            "disarmed_at": self.disarmed_at.isoformat() if self.disarmed_at is not None else None,
        })

    def _restore_journal(self) -> None:
        data = _read_json(self._journal)
        if data.get("trip_id") != self.trip_id:
            return
        self._intervals = [
            (_utc(datetime.fromisoformat(start)), _utc(datetime.fromisoformat(end)))
            for start, end in data["intervals"]
        ]
        self._faults = [
            _DeviceFault(_utc(datetime.fromisoformat(at)), device, unconfirmed)
            for at, device, unconfirmed in data.get("faults", ())
        ]
        self._shutdown_unconfirmed = data.get("shutdown_unconfirmed", False)
        disarmed_at = data.get("disarmed_at")
        if disarmed_at is not None:
            self._disarmed_at = _utc(datetime.fromisoformat(disarmed_at))
        run = data.get("open")
        if run is not None:
            start = _utc(datetime.fromisoformat(run["start"]))
            observed_through = _utc(datetime.fromisoformat(run["observed_through"]))
            self._record_interval(start, observed_through)
            self._save_journal()

    @property
    def disarmed_at(self) -> datetime | None:
        return self._disarmed_at

    @property
    def _effective_end(self) -> datetime:
        return self.disarmed_at or self._end

    def disarm(self, at: datetime) -> datetime | None:
        """Persist the first early end time and return it for future restarts."""
        instant = _utc(at)
        if self.disarmed_at is None and instant < self._end:
            self._disarmed_at = instant
            self._save_journal()
        return self.disarmed_at

    def record_device_fault(
        self, at: datetime, device: str = "plug", *, shutdown_unconfirmed: bool = False
    ) -> None:
        """Persist a device error and, when appropriate, latch uncertain shutdown."""
        self._faults.append(_DeviceFault(_utc(at), device, shutdown_unconfirmed))
        self._shutdown_unconfirmed |= shutdown_unconfirmed
        self._save_journal()

    def confirm_shutdown(self) -> None:
        """Clear shutdown uncertainty once all outputs acknowledge an off request."""
        if self._shutdown_unconfirmed:
            self._shutdown_unconfirmed = False
            self._save_journal()

    def _record_interval(self, start: datetime, end: datetime) -> None:
        start, end = max(start, self._start), min(end, self._effective_end)
        if start < end:
            self._intervals.append((start, end))

    def status(self, now: datetime, active: bool, room: str | None, *, force: bool = False) -> None:
        """Update the latest state and periodically checkpoint an active lamp run."""
        now_utc = _utc(now)
        if active and self._open is not None and room == self._open.room:
            observed_through = min(now_utc, self._effective_end)
            if observed_through - self._open.observed_through >= timedelta(minutes=1):
                self._open.observed_through = observed_through
                self._save_journal()
        disarmed = self.disarmed_at is not None
        state = active, room, disarmed, len(self._faults), self._shutdown_unconfirmed
        if force or self._last_status != state:
            if disarmed:
                message = "Vacation Ghost is disarmed."
            else:
                message = "Vacation Ghost is active." if active else "Vacation Ghost is idle."
            faults = _fault_summary(self._faults, self._shutdown_unconfirmed)
            _write_json(self.state_dir / "latest.json", {
                "schema_version": 1,
                "trip_id": self.trip_id,
                "updated_at": now_utc.astimezone(self._zone).isoformat(),
                "active": active,
                "lamp_room": room,
                "dry_run": self._dry_run,
                "disarmed": disarmed,
                **faults,
                "message": self._prefix + message + _fault_message(faults),
            })
            self._last_status = state

    def lamp_changed(self, at: datetime, room: str | None) -> None:
        """Record a successful transition to the given room or to all lamps off."""
        at = _utc(at)
        current_room = self._open.room if self._open is not None else None
        if room == current_room:
            return
        if self._open is not None:
            self._record_interval(self._open.start, max(at, self._open.observed_through))
        self._open = None if room is None else _LampRun(room, at, at)
        self._save_journal()

    def _metrics(self, start: datetime, end: datetime) -> dict:
        intervals = self._intervals.copy()
        if self._open is not None:
            intervals.append((self._open.start, self._open.observed_through))
        merged: list[tuple[datetime, datetime]] = []
        for lower, upper in sorted(intervals):
            lower, upper = max(start, lower), min(end, upper)
            if upper <= lower:
                continue
            if merged and lower <= merged[-1][1]:
                previous_lower, previous_upper = merged[-1]
                merged[-1] = (previous_lower, max(previous_upper, upper))
            else:
                merged.append((lower, upper))

        return {
            "lamp_seconds": int(sum((upper - lower).total_seconds() for lower, upper in merged)),
            "first_lamp_activity_at": merged[0][0].astimezone(self._zone).isoformat() if merged else None,
            "last_lamp_activity_at": merged[-1][1].astimezone(self._zone).isoformat() if merged else None,
        }

    def _night_window(self, night: date) -> tuple[datetime, datetime]:
        next_day = night + timedelta(days=1)
        start = _utc(datetime.combine(night, time(12), tzinfo=self._zone))
        end = _utc(datetime.combine(next_day, time(0), tzinfo=self._zone))
        return max(start, self._start), min(end, self._effective_end)

    def _write_report(self, path: Path, report_id: str, published_at: datetime, fields: dict) -> None:
        if _published_report_id(path) == report_id:
            return
        _write_json(path, {
            "schema_version": 1,
            "id": report_id,
            "trip_id": self.trip_id,
            "published_at": published_at.astimezone(self._zone).isoformat(),
            "dry_run": self._dry_run,
            **fields,
        })

    def _write_nightly_report(self, night: date, now: datetime) -> None:
        start, end = self._night_window(night)
        if start >= end:
            return
        metrics = self._metrics(start, end)
        first, last = metrics["first_lamp_activity_at"], metrics["last_lamp_activity_at"]
        if first is not None:
            first_time = _clock_time(datetime.fromisoformat(first))
            last_time = _clock_time(datetime.fromisoformat(last))
            message = (
                f"Lamps ran between {first_time} and {last_time} local time "
                f"({_duration(metrics['lamp_seconds'])} total)."
            )
        else:
            message = "No lamp activity was confirmed last night."
        delivery_date = night + timedelta(days=1)
        previous_morning = _utc(datetime.combine(night, time(8), tzinfo=self._zone))
        due = _utc(datetime.combine(delivery_date, time(8), tzinfo=self._zone))
        relevant_faults = [fault for fault in self._faults if previous_morning <= fault.at < due]
        faults = _fault_summary(relevant_faults, any(fault.shutdown_unconfirmed for fault in relevant_faults))
        self._write_report(self._daily / f"{delivery_date}.json", f"{self.trip_id}:{delivery_date}", now, {
            "night_of": night.isoformat(),
            "delivery_date": delivery_date.isoformat(),
            **metrics,
            **faults,
            "message": self._prefix + message + " Sound detection was off." + _fault_message(faults),
        })

    def _write_trip_report(self, now: datetime) -> None:
        metrics = self._metrics(self._start, self._effective_end)
        nights = 0
        night = self._start.astimezone(self._zone).date()
        while night <= self._effective_end.astimezone(self._zone).date():
            start, end = self._night_window(night)
            if self._metrics(start, end)["first_lamp_activity_at"] is not None:
                nights += 1
            night += timedelta(days=1)
        if metrics["first_lamp_activity_at"] is not None:
            night_label = "night" if nights == 1 else "nights"
            activity = (
                f"Lamps ran for {_duration(metrics['lamp_seconds'])} in total, "
                f"with evening activity on {nights} {night_label}."
            )
        else:
            activity = "No lamp activity was confirmed during the trip."
        early = self.disarmed_at is not None
        outcome = "Vacation Ghost was disarmed early." if early else "Vacation Ghost finished the trip."
        faults = _fault_summary(self._faults, self._shutdown_unconfirmed)
        self._write_report(self.state_dir / "trip.json", f"{self.trip_id}:trip", now, {
            **metrics,
            **faults,
            "ended_early": early,
            "nights_with_lamp_activity": nights,
            "message": f"{self._prefix}{outcome} {activity} Sound detection was off." + _fault_message(faults),
        })

    def publish_due(self, now: datetime) -> None:
        """Publish each elapsed 8 AM summary and the final summary once per trip."""
        now = _utc(now)
        daily_cutoff = min(now, self._effective_end)
        while self._next_night < daily_cutoff.astimezone(self._zone).date():
            morning = self._next_night + timedelta(days=1)
            due = _utc(datetime.combine(morning, time(8), tzinfo=self._zone))
            if due > daily_cutoff:
                break
            self._write_nightly_report(self._next_night, now)
            self._next_night = morning
        if now >= self._effective_end and not self._trip_published:
            self._write_trip_report(now)
            self._trip_published = True
