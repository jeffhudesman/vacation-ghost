"""Run a local trip, or replay its routine with simulated outputs."""

import logging
import math
from collections.abc import Callable
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
    def set_glow(self, rgb: tuple[int, int, int]) -> None: ...
    def play_sound(self, cue: str) -> None: ...
    def stop_sound(self) -> None: ...
    def poll_audio(self) -> None: ...


def utc(at: datetime) -> datetime:
    return at.astimezone(UTC)


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

    def _switch(self, room: str, on: bool) -> None:
        try:
            self.outputs.set_lamp(room, on)
        except (RuntimeError, OSError, ValueError) as exc:
            raise PlugError(f"{room}: {exc}") from exc

    def lamp(
        self, now: datetime, room: str | None, can_start: Callable[[], bool] | None = None
    ) -> None:
        if self.room == room:
            return
        if self.room is not None:
            self._switch(self.room, False)
            self.room = None
            self.reports.lamp_changed(now, None)
        if room is not None and (can_start is None or can_start()):
            self._switch(room, True)
            self.room = room
            self.reports.lamp_changed(now, room)

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

    def _try_peripheral(self, device: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except Exception as exc:
            logger.warning("%s: %s", device, exc)
            self.failed_peripherals.add(device)
            return False
        return True

    def peripheral(self, now: datetime, device: str, action: Callable[[], None]) -> None:
        if device not in self.failed_peripherals and not self._try_peripheral(device, action):
            self.reports.record_device_fault(now, device)

    def off(self, now: datetime, *, glow_pending: bool = False) -> bool:
        errors = []
        sound_off = self._try_peripheral("sound", self.outputs.stop_sound)
        glow_off = self._try_peripheral("glow", lambda: self.outputs.set_glow((0, 0, 0)))
        room_off = False
        for room in self.rooms:
            try:
                self._switch(room, False)
            except PlugError as exc:
                errors.append(str(exc))
            else:
                if room == self.room:
                    self.room = None
                    room_off = True
        self.scene = ""
        self.color = (0, 0, 0)
        if room_off:
            self.reports.lamp_changed(now, None)
        if not sound_off:
            self.reports.record_device_fault(now, "sound")
        if not glow_off:
            self.reports.record_device_fault(now, "glow")
        if errors:
            raise PlugError("; ".join(errors))
        return sound_off and (glow_off or not glow_pending)


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


def _shutdown(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime]
) -> None:
    reports = controller.reports
    for delay in (*RETRY_DELAYS, None):
        try:
            if controller.off(clock(), glow_pending=reservation.glow_pending):
                reservation.clear()
                reports.confirm_shutdown()
                return
        except PlugError as exc:
            logger.warning("Shutdown: %s", exc)
        reports.record_device_fault(clock(), "shutdown", shutdown_unconfirmed=True)
        reports.status(clock(), False, None, force=True)
        if delay is not None:
            sleep(delay)
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

    def can_start() -> bool:
        return (
            not stop.is_set()
            and reports.disarmed_at is None
            and (disarm_requested is None or disarm_requested() is None)
            and routine.active(clock())
        )

    reservation = reservation or OutputReservation(reports.state_dir)
    if reservation.engaged and not reservation.path.exists():
        reservation.engage()
    previous = clock()
    next_heartbeat = monotonic()
    initialized = False
    plug_failures = 0
    try:
        while not stop.is_set():
            now = clock()
            if (request := disarm_requested() if disarm_requested else None) is not None:
                reports.disarm(request)
            if reports.disarmed_at is not None or utc(now) >= routine.end:
                break

            try:
                if routine.active(now):
                    if not initialized:
                        reservation.engage()
                        controller.failed_peripherals.clear()
                        if controller.off(clock(), glow_pending=reservation.glow_pending):
                            reports.confirm_shutdown()
                        else:
                            reports.record_device_fault(clock(), "shutdown", shutdown_unconfirmed=True)
                        initialized = True
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
                reports.record_device_fault(clock(), "plug", shutdown_unconfirmed=True)
                reports.status(clock(), False, controller.room, force=True)
                initialized = False
                if plug_failures >= len(RETRY_DELAYS):
                    raise
                stop.wait(RETRY_DELAYS[plug_failures])
                plug_failures += 1
                previous = clock()
                continue

            tick = monotonic()
            heartbeat = tick >= next_heartbeat
            reports.status(clock(), initialized, controller.room, force=heartbeat)
            if heartbeat:
                reports.publish_due(clock())
                next_heartbeat = tick + HEARTBEAT.total_seconds()
            previous = now
            stop.wait(0.15 if initialized else 1)
    finally:
        try:
            if (request := disarm_requested() if disarm_requested else None) is not None:
                reports.disarm(request)
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
