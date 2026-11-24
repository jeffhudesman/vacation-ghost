"""Run a local trip, or replay its routine with simulated outputs."""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from random import Random
from threading import Event as StopEvent
from time import monotonic, sleep
from typing import Protocol

from .config import Config
from .reports import ReportStore
from .schedule import DayPlan, build_day
from .state import OutputReservation

logger = logging.getLogger(__name__)
HEARTBEAT = timedelta(seconds=15)
SOUND_GRACE = timedelta(seconds=4)
RETRY_DELAYS = (1, 3)


class PlugError(RuntimeError):
    """A relay state could not be confirmed."""


class Outputs(Protocol):
    def set_lamp(self, room: str, on: bool) -> None: ...
    def lamp_status(self, room: str) -> bool: ...
    def set_glow(self, rgb: tuple[int, int, int]) -> None: ...
    def play_sound(self, cue: str) -> None: ...
    def stop_sound(self) -> None: ...
    def poll_audio(self) -> None: ...


def utc(at: datetime) -> datetime:
    return at.astimezone(UTC)


def _switch(outputs: Outputs, room: str, on: bool) -> None:
    try:
        outputs.set_lamp(room, on)
    except (RuntimeError, OSError, ValueError) as exc:
        raise PlugError(f"{room}: {exc}") from exc


def _try_peripheral(device: str, action: Callable[[], None]) -> bool:
    try:
        action()
    except Exception as exc:
        logger.warning("%s: %s", device, exc)
        return False
    return True


@dataclass
class _OffResult:
    rooms_off: set[str]
    peripheral_faults: list[str]
    plug_errors: list[str]

    def confirmed(self, glow_pending: bool) -> bool:
        return not (
            self.plug_errors or "sound" in self.peripheral_faults
            or (glow_pending and "glow" in self.peripheral_faults)
        )


def _turn_outputs_off(rooms: tuple[str, str], outputs: Outputs) -> _OffResult:
    """Attempt every output with no dependency on report storage."""
    result = _OffResult(set(), [], [])
    for room in rooms:
        try:
            _switch(outputs, room, False)
        except PlugError as exc:
            result.plug_errors.append(str(exc))
        else:
            result.rooms_off.add(room)
    for device, action in (
        ("sound", outputs.stop_sound),
        ("glow", lambda: outputs.set_glow((0, 0, 0))),
    ):
        if not _try_peripheral(device, action):
            result.peripheral_faults.append(device)
    return result


class Routine:
    def __init__(self, config: Config):
        self.config = config
        self.start = utc(config.start)
        self.end = utc(config.end)
        self.day = lru_cache(maxsize=2)(self.day)

    def day(self, day: date) -> DayPlan:
        config = self.config
        return build_day(
            day,
            config.timezone,
            config.rooms,
            sunset_fallback=config.sunset,
            bedtime=config.bedtime,
            latitude=config.latitude,
            longitude=config.longitude,
            seed=config.seed,
        )

    def active(self, at: datetime) -> bool:
        return self.start <= utc(at) < self.end

    def desired(self, now: datetime, previous: datetime) -> tuple[str | None, str, str | None]:
        room, scene, cue = None, "", None
        now_utc = utc(now)
        latest_sound = max(utc(previous), now_utc - SOUND_GRACE)
        for event in self.day(now.date()).events:
            at = utc(event.at)
            if at > now_utc:
                break
            if event.kind == "lamp":
                room = event.value or None
            elif event.kind == "glow":
                scene = event.value
            elif event.kind == "sound" and self.start <= at and latest_sound < at:
                cue = event.value
        return room, scene, cue


class Controller:
    def __init__(self, rooms: tuple[str, str], outputs: Outputs, reports: ReportStore):
        self.rooms = rooms
        self.outputs = outputs
        self.reports = reports
        self.room: str | None = None
        self.scene = ""
        self.random = Random()
        self.color = (0, 0, 0)
        self.target = self.color
        self.next_cut = -math.inf
        self.failed_peripherals: set[str] = set()

    def lamp(
        self, now: datetime, room: str | None, can_start: Callable[[], bool] | None = None
    ) -> None:
        if self.room == room:
            return
        if self.room is not None:
            _switch(self.outputs, self.room, False)
            self.room = None
            self.reports.lamp_changed(now, None)
        if room is not None and (can_start is None or can_start()):
            _switch(self.outputs, room, True)
            self.room = room
            self.reports.lamp_changed(now, room)

    def verify_lamp(self, now: datetime) -> None:
        """Detect an unexpected relay change before applying the current scene."""
        room = self.room
        if room is None:
            return
        try:
            on = self.outputs.lamp_status(room)
        except (RuntimeError, OSError, ValueError) as exc:
            raise PlugError(f"{room}: {exc}") from exc
        if on:
            return
        logger.warning("Lamp in %s went off during an active scene.", room)
        self.room = None
        self.reports.lamp_changed(now, None)
        self.reports.record_device_fault(now, "plug")
        for other in self.rooms:
            if other != room:
                _switch(self.outputs, other, False)

    def glow(self, now: datetime, scene: str) -> None:
        if not scene:
            if self.scene:
                self.outputs.set_glow((0, 0, 0))
            self.scene = ""
            self.color = (0, 0, 0)
            return
        frame_at = now.timestamp()
        if scene != self.scene or frame_at >= self.next_cut:
            if scene == "sports":
                palette = ((17, 24, 17), (15, 22, 32), (35, 30, 21), (14, 28, 33))
                delay = self.random.uniform(0.7, 2.0)
            else:
                palette = ((9, 11, 22), (23, 13, 10), (6, 9, 14), (18, 18, 22))
                delay = self.random.uniform(2.0, 6.0)
            self.target = self.random.choice(palette)
            self.next_cut = frame_at + delay
        color = tuple(round((source + target) / 2) for source, target in zip(self.color, self.target))
        if color != self.color:
            self.outputs.set_glow(color)
            self.color = color
        self.scene = scene

    def peripheral(self, now: datetime, device: str, action: Callable[[], None]) -> None:
        if device not in self.failed_peripherals and not _try_peripheral(device, action):
            self.failed_peripherals.add(device)
            self.reports.record_device_fault(now, device)

    def record_off(self, now: datetime, result: _OffResult) -> None:
        self.scene = ""
        self.color = (0, 0, 0)
        self.failed_peripherals.update(result.peripheral_faults)
        if self.room in result.rooms_off:
            self.room = None
            self.reports.lamp_changed(now, None)
        for device in result.peripheral_faults:
            self.reports.record_device_fault(now, device)

    def off(self, now: datetime, *, glow_pending: bool = False) -> bool:
        result = _turn_outputs_off(self.rooms, self.outputs)
        self.record_off(now, result)
        if result.plug_errors:
            raise PlugError("; ".join(result.plug_errors))
        return result.confirmed(glow_pending)


def simulate(config: Config, outputs: Outputs, reports: ReportStore, through: datetime) -> None:
    routine = Routine(config)
    controller = Controller(config.rooms, outputs, reports)
    through = through.astimezone(config.timezone)
    cutoff = min(utc(through), routine.end)
    if cutoff >= routine.start:
        room, scene, _ = routine.desired(config.start, config.start)
        controller.lamp(config.start, room)
        controller.glow(config.start, scene)
    day = config.start.date()
    while day <= through.date() and day <= config.end.date():
        for event in routine.day(day).events:
            at = utc(event.at)
            if not routine.start <= at < routine.end or at > cutoff:
                continue
            if event.kind == "lamp":
                controller.lamp(event.at, event.value or None)
            elif event.kind == "glow":
                controller.glow(event.at, event.value)
            elif event.kind == "sound":
                outputs.play_sound(event.value)
        day += timedelta(days=1)
    if utc(through) >= routine.end:
        controller.off(config.end)
    reports.status(through, routine.active(through), controller.room)
    reports.publish_due(through)


def _retry_outputs_off(
    rooms: tuple[str, str], outputs: Outputs, *, glow_pending: bool, clock: Callable[[], datetime]
) -> list[tuple[datetime, _OffResult]]:
    attempts = []
    for delay in (*RETRY_DELAYS, None):
        result = _turn_outputs_off(rooms, outputs)
        attempts.append((clock(), result))
        if result.confirmed(glow_pending):
            break
        if result.plug_errors:
            logger.warning("Shutdown: %s", "; ".join(result.plug_errors))
        if delay is not None:
            sleep(delay)
    return attempts


def recover_outputs(
    rooms: tuple[str, str], outputs: Outputs, reservation: OutputReservation
) -> bool:
    """Recover a pending shutdown without constructing or updating reports."""
    if not reservation.engaged:
        return False
    attempts = _retry_outputs_off(rooms, outputs, glow_pending=reservation.glow_pending,
                                  clock=lambda: datetime.now(UTC))
    if not attempts[-1][1].confirmed(reservation.glow_pending):
        raise RuntimeError("shutdown unconfirmed; retry with the same configuration and state directory")
    reservation.clear()
    return True


def _shutdown(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime]
) -> None:
    glow_pending = reservation.glow_pending
    attempts = _retry_outputs_off(controller.rooms, controller.outputs, glow_pending=glow_pending, clock=clock)
    confirmed = attempts[-1][1].confirmed(glow_pending)
    if confirmed:
        reservation.clear()
    reports = controller.reports
    recorded_end = reports.disarmed_at or reports.completed_at
    for at, result in attempts:
        if recorded_end is not None:
            at = max(utc(at), recorded_end)
        controller.record_off(at, result)
        if not result.confirmed(glow_pending):
            reports.record_device_fault(at, "shutdown", shutdown_unconfirmed=True)
    if confirmed:
        reports.confirm_shutdown()
    else:
        raise RuntimeError("shutdown unconfirmed; retry with the same configuration and state directory")


def run(
    config: Config,
    controller: Controller,
    stop: StopEvent,
    *,
    reservation: OutputReservation | None = None,
    disarm_requested: Callable[[], datetime | None] | None = None,
) -> None:
    routine = Routine(config)
    reports = controller.reports

    def clock() -> datetime:
        return datetime.now(config.timezone)

    def trip_finished(at: datetime) -> bool:
        if reports.completed_at is not None or reports.disarmed_at is not None:
            return True
        if (request := disarm_requested() if disarm_requested else None) is not None:
            reports.disarm(request)
        return reports.disarmed_at is not None or reports.complete(at) is not None

    def can_start() -> bool:
        if stop.is_set() or trip_finished(clock()):
            return False
        at = clock()
        return reports.complete(at) is None and not stop.is_set() and routine.active(at)

    reservation = reservation or OutputReservation(reports.state_dir)
    previous = clock()
    next_heartbeat = monotonic()
    initialized = False
    plug_failures = 0
    try:
        if trip_finished(previous):
            return
        if reservation.engaged and not reservation.path.exists():
            reservation.engage()
        while not stop.is_set():
            now = clock()
            if trip_finished(now):
                break

            tick = monotonic()
            heartbeat = tick >= next_heartbeat
            try:
                if routine.active(now):
                    if not initialized:
                        reservation.engage()
                        reset_at = clock()
                        if trip_finished(reset_at):
                            break
                        controller.failed_peripherals.clear()
                        if controller.off(reset_at, glow_pending=reservation.glow_pending):
                            reports.confirm_shutdown()
                        else:
                            reports.record_device_fault(clock(), "shutdown", shutdown_unconfirmed=True)
                        initialized = True
                    elif heartbeat and can_start():
                        controller.verify_lamp(now)
                    if can_start():
                        now = clock()
                        room, scene, cue = routine.desired(now, previous)
                        if not scene:
                            controller.peripheral(now, "glow", lambda: controller.glow(now, ""))
                        controller.lamp(now, room, can_start)
                        if scene and "glow" not in controller.failed_peripherals:
                            if config.hardware is None or config.hardware.led_count:
                                reservation.before_glow()
                            if can_start():
                                controller.peripheral(clock(), "glow", lambda: controller.glow(clock(), scene))
                        if can_start() and cue is not None:
                            controller.peripheral(clock(), "sound", lambda: controller.outputs.play_sound(cue))
                        controller.peripheral(clock(), "sound", controller.outputs.poll_audio)
                elif reservation.engaged:
                    _shutdown(controller, reservation, clock)
                    initialized = False
                plug_failures = 0
            except PlugError as exc:
                logger.warning("Plug fault: %s", exc)
                fault_at = clock()
                ended = trip_finished(fault_at)
                reports.record_device_fault(fault_at, "plug", shutdown_unconfirmed=True)
                reports.status(fault_at, False, None, force=True)
                if ended:
                    break
                initialized = False
                if plug_failures >= len(RETRY_DELAYS):
                    raise
                stop.wait(RETRY_DELAYS[plug_failures])
                plug_failures += 1
                previous = clock()
                continue

            report_at = clock()
            if trip_finished(report_at):
                break
            reports.status(report_at, initialized, controller.room, force=heartbeat)
            if heartbeat:
                reports.publish_due(report_at)
                next_heartbeat = tick + HEARTBEAT.total_seconds()
            previous = now
            stop.wait(0.15 if initialized else 1)
    finally:
        try:
            trip_finished(clock())
        finally:
            _finish(controller, reservation, clock)


def _finish(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime]
) -> None:
    try:
        if reservation.engaged:
            _shutdown(controller, reservation, clock)
    finally:
        controller.reports.status(clock(), False, None, force=True)
        controller.reports.publish_due(clock())


def rehearse(
    config: Config, controller: Controller, stop: StopEvent, reservation: OutputReservation
) -> None:
    """Exercise each output in order, regardless of the configured trip dates."""
    def clock() -> datetime:
        return datetime.now(config.timezone)

    def can_start() -> bool:
        return not stop.is_set()

    reservation.engage()
    try:
        controller.off(clock(), glow_pending=reservation.glow_pending)
        for room, cue, scene in zip(config.rooms, ("kettle", "tv"), ("", "drama")):
            if not can_start():
                break
            controller.lamp(clock(), room, can_start)
            if not can_start():
                break
            controller.peripheral(clock(), "sound", lambda: controller.outputs.play_sound(cue))
            if scene and "glow" not in controller.failed_peripherals and (config.hardware is None or config.hardware.led_count):
                reservation.before_glow()
            for _ in range(30):
                if stop.wait(0.2):
                    break
                controller.peripheral(clock(), "glow", lambda: controller.glow(clock(), scene))
                controller.peripheral(clock(), "sound", controller.outputs.poll_audio)
        if controller.failed_peripherals:
            raise RuntimeError("rehearsal found faults in " + ", ".join(sorted(controller.failed_peripherals)))
    finally:
        _shutdown(controller, reservation, clock)
