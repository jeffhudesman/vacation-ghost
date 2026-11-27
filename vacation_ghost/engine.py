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
from .schedule import DayPlan, build_config_day
from .state import OutputReservation

logger = logging.getLogger(__name__)
HEARTBEAT = timedelta(seconds=15)
SOUND_GRACE = timedelta(seconds=4)
RETRY_DELAYS = (1, 3)
DARK = (0, 0, 0)


class PlugError(RuntimeError):
    """A relay state could not be confirmed."""


class PlugIdentityError(PlugError):
    """A plug could not be identified before starting the routine."""


class PlugPairingError(PlugIdentityError):
    """The plug identities require an explicit commissioning change."""


class RehearsalIncomplete(RuntimeError):
    """The operator interrupted the rehearsal before every stage finished."""


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


def _lamp_status(outputs: Outputs, room: str) -> bool:
    try:
        return outputs.lamp_status(room)
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
    rooms_off: dict[str, datetime]
    peripheral_faults: list[str]
    plug_errors: list[str]

    def confirmed(self, glow_pending: bool) -> bool:
        return not (
            self.plug_errors or "sound" in self.peripheral_faults
            or (glow_pending and "glow" in self.peripheral_faults)
        )

    def led_black_frame_sent(self, led_enabled: bool) -> bool | None:
        return "glow" not in self.peripheral_faults if led_enabled else None


def _turn_outputs_off(
    rooms: tuple[str, str], outputs: Outputs, clock: Callable[[], datetime]
) -> _OffResult:
    """Attempt every output with no dependency on report storage."""
    result = _OffResult({}, [], [])
    for room in rooms:
        try:
            _switch(outputs, room, False)
        except PlugError as exc:
            result.plug_errors.append(str(exc))
        else:
            result.rooms_off[room] = clock()
    for device, action in (
        ("sound", outputs.stop_sound),
        ("glow", lambda: outputs.set_glow(DARK)),
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
        return build_config_day(self.config, day)

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
        self.startup_error: str | None = None
        self.scene = ""
        self.random = Random()
        self.color = DARK
        self.target = self.color
        self.next_cut = -math.inf
        self.failed_peripherals: set[str] = set()
        self._blackout_at = math.inf
        self._blackout_failures = 0
        self._blackout_owns_uncertainty = False

    def lamp(
        self, now: datetime, room: str | None, can_start: Callable[[], bool] | None = None,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        previous_room = self.room
        if previous_room == room:
            return
        confirmed_at = clock or (lambda: now)
        if previous_room is not None:
            _switch(self.outputs, previous_room, False)
            self.room = None
            self.reports.lamp_changed(confirmed_at(), None)
        if room is None:
            return
        if clock is not None:
            for other in self.rooms:
                if other not in (room, previous_room) and self._switch_off_unexpected_lamp(other):
                    self.reports.record_device_fault(confirmed_at(), "plug")
        def allow_activation() -> bool:
            if can_start is not None and not can_start():
                return False
            self.reports.set_shutdown_checks(None, self.reports.led_black_frame_sent)
            return True

        guarded_switch = getattr(self.outputs, "set_lamp_if", None)
        if guarded_switch is None:
            if not allow_activation():
                return
            _switch(self.outputs, room, True)
        else:
            try:
                if not guarded_switch(room, allow_activation):
                    return
            except (RuntimeError, OSError, ValueError) as exc:
                raise PlugError(f"{room}: {exc}") from exc
        self.room = room
        self.reports.lamp_changed(confirmed_at(), room)

    def _switch_off_unexpected_lamp(self, room: str) -> bool:
        if not _lamp_status(self.outputs, room):
            return False
        logger.warning("Lamp in %s switched on outside its scheduled scene.", room)
        _switch(self.outputs, room, False)
        return True

    def verify_lamps(self, clock: Callable[[], datetime]) -> None:
        """Reconcile both relays, including when the schedule is quiet."""
        unexpected = False
        for room in self.rooms:
            if room == self.room:
                if _lamp_status(self.outputs, room):
                    continue
                logger.warning("Lamp in %s went off during an active scene.", room)
                self.room = None
                self.reports.lamp_changed(clock(), None)
                unexpected = True
            elif self._switch_off_unexpected_lamp(room):
                unexpected = True
        if unexpected:
            self.reports.record_device_fault(clock(), "plug")

    def record_plug_fault(self, now: datetime) -> None:
        self._blackout_owns_uncertainty = False
        self.reports.set_shutdown_checks(None, self.reports.led_black_frame_sent)
        self.reports.record_device_fault(now, "plug", shutdown_unconfirmed=True)

    def glow(self, now: datetime, scene: str) -> None:
        if not scene:
            if self.scene:
                self.outputs.set_glow(DARK)
                self.reports.set_shutdown_checks(self.reports.relay_off_verified, True)
            self.scene = ""
            self.color = DARK
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
            self.reports.set_shutdown_checks(self.reports.relay_off_verified, None)
            self.outputs.set_glow(color)
            self.color = color
        self.scene = scene

    def update_glow(self, now: datetime, scene: str) -> None:
        if "glow" in self.failed_peripherals:
            self._retry_blackout()
        elif not _try_peripheral("glow", lambda: self.glow(now, scene)):
            self.failed_peripherals.add("glow")
            self._blackout_at = -math.inf
            self._retry_blackout()
            self.reports.record_device_fault(
                now, "glow", shutdown_unconfirmed=bool(self._blackout_failures), affects_shutdown_status=False
            )

    def _retry_blackout(self) -> None:
        if monotonic() < self._blackout_at:
            return
        if _try_peripheral("glow blackout", lambda: self.outputs.set_glow(DARK)):
            self._blackout_at = math.inf
            self._blackout_failures = 0
            self.scene = ""
            self.color = DARK
            self.reports.set_shutdown_checks(self.reports.relay_off_verified, True)
            if self._blackout_owns_uncertainty:
                self._blackout_owns_uncertainty = False
                self.reports.confirm_shutdown(
                    relay_off_verified=self.reports.relay_off_verified, led_black_frame_sent=True,
                )
            return

        self.reports.set_shutdown_checks(self.reports.relay_off_verified, False)
        if self._blackout_failures == 0:
            self._blackout_owns_uncertainty = not self.reports.shutdown_unconfirmed
            self.reports.set_shutdown_unconfirmed(True)
        if self._blackout_failures < len(RETRY_DELAYS):
            self._blackout_at = monotonic() + RETRY_DELAYS[self._blackout_failures]
        else:
            self._blackout_at = math.inf
        self._blackout_failures += 1

    def peripheral(self, now: datetime, device: str, action: Callable[[], None]) -> None:
        if device not in self.failed_peripherals and not _try_peripheral(device, action):
            self.failed_peripherals.add(device)
            self.reports.record_device_fault(now, device)

    def play_cue(self, now: datetime, cue: str, can_start: Callable[[], bool]) -> bool:
        started = False

        def play() -> None:
            nonlocal started
            guarded_play = getattr(self.outputs, "play_sound_if", None)
            if guarded_play is not None:
                started = guarded_play(cue, can_start)
            elif can_start():
                self.outputs.play_sound(cue)
                started = True

        self.peripheral(now, "sound", play)
        return started

    def record_off(self, now: datetime, result: _OffResult) -> None:
        if "glow" not in result.peripheral_faults:
            self.scene = ""
            self.color = DARK
            self._blackout_at = math.inf
            self._blackout_failures = 0
        self._blackout_owns_uncertainty = False
        self.failed_peripherals.update(result.peripheral_faults)
        if self.room in result.rooms_off:
            confirmed_at = result.rooms_off[self.room]
            recorded_end = self.reports.disarmed_at or self.reports.completed_at
            if recorded_end is not None:
                confirmed_at = max(utc(confirmed_at), recorded_end)
            self.room = None
            self.reports.lamp_changed(confirmed_at, None)
        for device in result.peripheral_faults:
            self.reports.record_device_fault(now, device)

    def off(
        self, now: datetime, *, glow_pending: bool = False, led_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> bool:
        confirmed_at = clock or (lambda: now)
        result = _turn_outputs_off(self.rooms, self.outputs, confirmed_at)
        confirmed = result.confirmed(glow_pending)
        led_black_frame_sent = result.led_black_frame_sent(led_enabled or glow_pending)
        self.reports.set_shutdown_checks(not result.plug_errors, led_black_frame_sent)
        self.reports.set_shutdown_unconfirmed(not confirmed)
        self.record_off(now, result)
        if confirmed:
            self.reports.confirm_shutdown(relay_off_verified=True, led_black_frame_sent=led_black_frame_sent)
        if glow_pending and "glow" in result.peripheral_faults:
            if not self._blackout_failures:
                self._blackout_at = monotonic() + RETRY_DELAYS[0]
                self._blackout_failures = 1
            self._blackout_owns_uncertainty = not result.plug_errors and "sound" not in result.peripheral_faults
        if result.plug_errors:
            raise PlugError("; ".join(result.plug_errors))
        return confirmed


def simulate(config: Config, outputs: Outputs, reports: ReportStore, through: datetime) -> None:
    routine = Routine(config)
    controller = Controller(config.rooms, outputs, reports)
    through = through.astimezone(config.timezone)
    cutoff = min(utc(through), routine.end)
    led_enabled = config.hardware is None or bool(config.hardware.led_count)
    sound_enabled = config.hardware is None or bool(config.hardware.volume)
    if cutoff >= routine.start:
        room, scene, _ = routine.desired(config.start, config.start)
        controller.lamp(config.start, room)
        if led_enabled:
            controller.glow(config.start, scene)
    day = config.start.date()
    while day <= through.date() and day <= config.end.date():
        plan = routine.day(day)
        if cutoff >= routine.start and plan.evening_note is not None:
            reports.record_schedule_note(day, plan.evening_note)
        for event in plan.events:
            at = utc(event.at)
            if not routine.start <= at < routine.end or at > cutoff:
                continue
            if event.kind == "lamp":
                controller.lamp(event.at, event.value or None)
            elif event.kind == "glow" and led_enabled:
                controller.glow(event.at, event.value)
            elif event.kind == "sound" and sound_enabled:
                outputs.play_sound(event.value)
        day += timedelta(days=1)
    if utc(through) >= routine.end:
        reports.complete(config.end)
        controller.off(config.end, led_enabled=led_enabled)
    reports.status(through, routine.active(through), controller.room)
    reports.publish_due(through)


def _retry_outputs_off(
    rooms: tuple[str, str], outputs: Outputs, *, glow_pending: bool, clock: Callable[[], datetime]
) -> list[tuple[datetime, _OffResult]]:
    attempts = []
    for delay in (*RETRY_DELAYS, None):
        result = _turn_outputs_off(rooms, outputs, clock)
        attempts.append((clock(), result))
        if result.confirmed(glow_pending):
            break
        if result.plug_errors:
            logger.warning("Shutdown: %s", "; ".join(result.plug_errors))
        if delay is not None:
            sleep(delay)
    return attempts


def recover_outputs(
    rooms: tuple[str, str], outputs: Outputs, reservation: OutputReservation, *, led_enabled: bool = False,
) -> bool:
    """Recover a pending shutdown without constructing or updating reports."""
    if reservation.shutdown_confirmed:
        if reservation.engaged:
            reservation.clear()
        return True
    if not reservation.engaged:
        return False
    attempts = _retry_outputs_off(rooms, outputs, glow_pending=reservation.glow_pending,
                                  clock=lambda: datetime.now(UTC))
    if not attempts[-1][1].confirmed(reservation.glow_pending):
        raise RuntimeError("shutdown unconfirmed; retry with the same configuration and state directory")
    reservation.confirm_shutdown(led_black_frame_sent=attempts[-1][1].led_black_frame_sent(
        led_enabled or reservation.glow_pending,
    ))
    return True


def _shutdown(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime], *, led_enabled: bool,
) -> None:
    glow_pending = reservation.glow_pending
    attempts = _retry_outputs_off(controller.rooms, controller.outputs, glow_pending=glow_pending, clock=clock)
    final_result = attempts[-1][1]
    confirmed = final_result.confirmed(glow_pending)
    led_black_frame_sent = final_result.led_black_frame_sent(led_enabled or glow_pending)
    reports = controller.reports
    reports.set_shutdown_checks(not final_result.plug_errors, led_black_frame_sent)
    reports.set_shutdown_unconfirmed(not confirmed)
    if confirmed:
        reservation.confirm_shutdown(led_black_frame_sent=led_black_frame_sent)
    recorded_end = reports.disarmed_at or reports.completed_at
    for at, result in attempts:
        if recorded_end is not None:
            at = max(utc(at), recorded_end)
        controller.record_off(at, result)
        if not result.confirmed(glow_pending):
            reports.record_device_fault(at, "shutdown", shutdown_unconfirmed=True, affects_shutdown_status=False)
    if confirmed:
        reports.confirm_shutdown(relay_off_verified=True, led_black_frame_sent=led_black_frame_sent)
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
    led_enabled = config.hardware is None or bool(config.hardware.led_count)

    def clock() -> datetime:
        return datetime.now(config.timezone)

    def trip_finished(at: datetime) -> bool:
        if reports.completed_at is not None or reports.disarmed_at is not None:
            return True
        if (request := disarm_requested() if disarm_requested else None) is not None:
            reports.disarm(request)
        return reports.disarmed_at is not None or reports.complete(at) is not None

    def active_at() -> datetime | None:
        if stop.is_set() or trip_finished(clock()):
            return None
        at = clock()
        if reports.complete(at) is None and not stop.is_set() and routine.active(at):
            return at
        return None

    def lamp_is_current(room: str | None) -> bool:
        at = active_at()
        return at is not None and routine.desired(at, at)[0] == room

    def cue_is_current(cue: str) -> bool:
        at = active_at()
        return at is not None and routine.desired(at, last_sound_check)[2] == cue

    reservation = reservation or OutputReservation(reports.state_dir)
    previous = clock()
    last_sound_check = utc(previous)
    next_heartbeat = monotonic()
    initialized = False
    plug_failures = 0
    try:
        finished = trip_finished(previous)
        if reservation.shutdown_confirmed:
            if reservation.engaged:
                reservation.clear()
            _publish_stopped(controller, reservation, clock)
        if finished:
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
                        controller.startup_error = None
                        reset_at = clock()
                        if trip_finished(reset_at):
                            break
                        if not controller.off(reset_at, glow_pending=reservation.glow_pending,
                                              led_enabled=led_enabled, clock=clock):
                            reports.record_device_fault(clock(), "shutdown", shutdown_unconfirmed=True)
                        initialized = True
                    elif heartbeat and active_at() is not None:
                        controller.verify_lamps(clock)
                    if (scene_at := active_at()) is not None:
                        plan = routine.day(scene_at.date())
                        if plan.evening_note is not None:
                            reports.record_schedule_note(plan.day, plan.evening_note)
                        room, scene, _ = routine.desired(scene_at, scene_at)
                        if led_enabled and (not scene or "glow" in controller.failed_peripherals):
                            controller.update_glow(scene_at, "")
                        controller.lamp(scene_at, room, lambda: lamp_is_current(room), clock=clock)
                        if led_enabled and scene and "glow" not in controller.failed_peripherals:
                            reservation.before_glow()
                            glow_at = active_at()
                            if glow_at is not None and routine.desired(glow_at, glow_at)[:2] == (controller.room, scene):
                                controller.update_glow(glow_at, scene)
                        if (sound_at := active_at()) is not None:
                            cue = routine.desired(sound_at, last_sound_check)[2]
                            if cue is not None:
                                controller.play_cue(sound_at, cue, lambda: cue_is_current(cue))
                            last_sound_check = max(last_sound_check, utc(sound_at))
                        controller.peripheral(clock(), "sound", controller.outputs.poll_audio)
                elif reservation.engaged:
                    _shutdown(controller, reservation, clock, led_enabled=led_enabled)
                    _publish_stopped(controller, reservation, clock)
                    initialized = False
                plug_failures = 0
            except PlugError as exc:
                logger.warning("Plug fault: %s", exc)
                fault_at = clock()
                ended = trip_finished(fault_at)
                if isinstance(exc, PlugIdentityError):
                    controller.startup_error = str(exc)
                    reports.record_device_fault(fault_at, "plug")
                else:
                    controller.record_plug_fault(fault_at)
                reports.status(fault_at, False, None, force=True, startup_error=controller.startup_error)
                if ended or stop.is_set():
                    break
                if isinstance(exc, PlugPairingError):
                    raise
                initialized = False
                if plug_failures >= len(RETRY_DELAYS):
                    raise
                stop.wait(RETRY_DELAYS[plug_failures])
                plug_failures += 1
                last_sound_check = max(last_sound_check, utc(clock()))
                continue

            report_at = clock()
            if trip_finished(report_at):
                break
            reports.status(report_at, initialized, controller.room, force=heartbeat)
            if heartbeat:
                reports.publish_due(report_at)
                next_heartbeat = tick + HEARTBEAT.total_seconds()
            if not routine.active(now):
                last_sound_check = max(last_sound_check, utc(now))
            stop.wait(0.15 if initialized else 1)
    finally:
        try:
            trip_finished(clock())
        finally:
            _finish(controller, reservation, clock, led_enabled=led_enabled)


def _finish(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime], *, led_enabled: bool,
) -> None:
    try:
        if reservation.engaged:
            if reservation.shutdown_confirmed:
                reservation.clear()
            else:
                _shutdown(controller, reservation, clock, led_enabled=led_enabled)
    finally:
        _publish_stopped(controller, reservation, clock)


def _publish_stopped(
    controller: Controller, reservation: OutputReservation, clock: Callable[[], datetime],
) -> None:
    reports = controller.reports
    confirmed = reservation.shutdown_confirmed
    try:
        if confirmed:
            reports.confirm_shutdown(relay_off_verified=True, led_black_frame_sent=reservation.led_black_frame_sent)
    finally:
        reports.status(clock(), False, None, force=True, startup_error=controller.startup_error)
        reports.publish_due(clock())
    if confirmed and not reservation.engaged:
        reservation.clear_shutdown_confirmation()


def rehearse(
    config: Config, controller: Controller, stop: StopEvent, reservation: OutputReservation
) -> str:
    """Exercise each output in order, regardless of the configured trip dates."""
    def clock() -> datetime:
        return datetime.now(config.timezone)

    def can_start() -> bool:
        return not stop.is_set()

    led_enabled = config.hardware is None or bool(config.hardware.led_count)
    sound_enabled = config.hardware is None or bool(config.hardware.volume)
    lamps: list[str] = []
    cues: list[str] = []
    color_sent = False
    finished = False
    reservation.engage()
    try:
        controller.off(clock(), glow_pending=reservation.glow_pending, led_enabled=led_enabled, clock=clock)
        for room, cue, scene in zip(config.rooms, ("kettle", "tv"), ("", "drama")):
            if not can_start():
                break
            controller.lamp(clock(), room, can_start, clock=clock)
            if controller.room == room:
                lamps.append(room)
            if not can_start():
                break
            if sound_enabled and controller.play_cue(clock(), cue, can_start):
                cues.append(cue)
            if scene and "glow" not in controller.failed_peripherals and led_enabled:
                reservation.before_glow()
            for _ in range(30):
                if stop.wait(0.2):
                    break
                if led_enabled:
                    controller.update_glow(clock(), scene)
                    if controller.color != DARK:
                        color_sent = True
                controller.peripheral(clock(), "sound", controller.outputs.poll_audio)
            if stop.is_set():
                break
        else:
            finished = True
    finally:
        outcome = "failed"
        try:
            _shutdown(controller, reservation, clock, led_enabled=led_enabled)
        except Exception:
            try:
                controller.reports.write_rehearsal(clock(), outcome, lamps, cues, color_sent)
            except (OSError, ValueError) as report_error:
                logger.warning("Could not save the failed rehearsal result: %s", report_error)
            raise
        if not controller.failed_peripherals:
            if finished:
                outcome = "completed"
            elif stop.is_set():
                outcome = "interrupted"
        controller.reports.write_rehearsal(clock(), outcome, lamps, cues, color_sent)
        reservation.clear_shutdown_confirmation()
    if controller.failed_peripherals:
        raise RuntimeError("rehearsal found faults in " + ", ".join(sorted(controller.failed_peripherals)))
    exercised = (
        f"Lamps confirmed on: {', '.join(lamps) or 'none'}. "
        f"Sound cues started: {', '.join(cues) or 'none'}. "
        f"Color frame sent: {'yes' if color_sent else 'no'}."
    )
    if not finished:
        raise RehearsalIncomplete("Rehearsal interrupted. " + exercised)
    return exercised
