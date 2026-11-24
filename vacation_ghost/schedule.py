"""Create a day of occupancy cues using only local configuration."""

import calendar
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: Literal["lamp", "glow", "sound"]
    value: str


@dataclass(frozen=True)
class DayPlan:
    day: date
    sunset: datetime
    events: tuple[Event, ...]


def _local_sunset(day: date, tz: ZoneInfo, latitude: float, longitude: float) -> datetime | None:
    """Estimate sunset with NOAA's equation of time and solar declination."""
    days = 366 if calendar.isleap(day.year) else 365
    angle = math.tau * (day.timetuple().tm_yday - 1) / days
    equation_of_time = 229.18 * (
        0.000075 + 0.001868 * math.cos(angle) - 0.032077 * math.sin(angle)
        - 0.014615 * math.cos(2 * angle) - 0.040849 * math.sin(2 * angle)
    )
    declination = (
        0.006918 - 0.399912 * math.cos(angle) + 0.070257 * math.sin(angle)
        - 0.006758 * math.cos(2 * angle) + 0.000907 * math.sin(2 * angle)
        - 0.002697 * math.cos(3 * angle) + 0.00148 * math.sin(3 * angle)
    )
    lat = math.radians(latitude)
    denominator = math.cos(lat) * math.cos(declination)
    if math.isclose(denominator, 0, abs_tol=1e-12):
        return None
    # The 90.833 degree zenith includes atmospheric refraction and disk radius.
    hour_angle_cos = (math.cos(math.radians(90.833)) - math.sin(lat) * math.sin(declination)) / denominator
    if not -1 <= hour_angle_cos <= 1:
        return None
    hour_angle = math.degrees(math.acos(hour_angle_cos))
    minutes_utc = 720 - 4 * longitude - equation_of_time + 4 * hour_angle
    instant = datetime.combine(day, time(), UTC) + timedelta(minutes=round(minutes_utc))
    # Civil time zones across the date line can label the same solar cycle differently.
    local_date = instant.astimezone(tz).date()
    return (instant + (day - local_date)).astimezone(tz)


def build_day(
    day: date,
    tz: ZoneInfo,
    rooms: tuple[str, str],
    *,
    sunset_fallback: time,
    bedtime: time,
    latitude: float | None = None,
    longitude: float | None = None,
    seed: str = "vacation-ghost",
) -> DayPlan:
    """Build a repeatable local-day plan with the kitchen followed by the living room.

    A lamp event declares the only room that should be lit. An empty value
    turns all lamps or the TV glow off, according to the event kind.
    """
    if sunset_fallback.tzinfo is not None or bedtime.tzinfo is not None:
        raise ValueError("Sunset fallback and bedtime must use local clock times")
    if not time(18) <= bedtime <= time(23, 55):
        raise ValueError("Bedtime must be between 18:00 and 23:55")
    if (latitude is None) != (longitude is None):
        raise ValueError("Sunset calculation needs both latitude and longitude")

    kitchen, living_room = rooms
    if not kitchen or not living_room or kitchen == living_room:
        raise ValueError("The two rooms must have distinct names")
    rng = random.Random(f"{seed}/{day.isoformat()}")
    sunset = None
    if latitude is not None and longitude is not None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Latitude or longitude is out of range")
        sunset = _local_sunset(day, tz, latitude, longitude)
    if sunset is None:
        sunset = datetime.combine(day, sunset_fallback, tz)

    morning_start = datetime.combine(day, time(7), tz) + timedelta(minutes=rng.randint(0, 52))
    morning_events = [
        Event(morning_start, "lamp", kitchen),
        Event(morning_start + timedelta(minutes=rng.randint(1, 5)), "sound", "kettle"),
        Event(morning_start + timedelta(minutes=rng.randint(15, 25)), "lamp", ""),
    ]
    if rng.random() < 0.45:
        morning_events.append(Event(morning_start + timedelta(minutes=rng.randint(7, 11)), "sound", "cabinet"))

    nominal_bedtime = datetime.combine(day, bedtime, tz)
    night_end = min(
        nominal_bedtime + timedelta(minutes=rng.randint(-24, 24)),
        datetime.combine(day, time(23, 55), tz),
    )
    night_start = min(
        max(sunset + timedelta(minutes=rng.randint(-14, 16)), datetime.combine(day, time(15), tz)),
        night_end - timedelta(minutes=75),
    )
    evening_length = night_end - night_start
    first_tv = night_start + evening_length * rng.uniform(0.12, 0.19)
    kitchen_break = night_start + evening_length * rng.uniform(0.56, 0.67)
    second_tv = kitchen_break + timedelta(minutes=rng.randint(6, 14))
    tv_off = night_start + evening_length * rng.uniform(0.88, 0.95)
    first_scene = rng.choice(("drama", "drama", "sports"))
    second_scene = rng.choice(("drama", "sports"))

    evening_events = [
        Event(night_start, "lamp", kitchen),
        Event(first_tv, "lamp", living_room),
        Event(first_tv + timedelta(minutes=rng.randint(1, 5)), "glow", first_scene),
        Event(first_tv + timedelta(minutes=rng.randint(6, 11)), "sound", "tv"),
        Event(kitchen_break, "glow", ""),
        Event(kitchen_break, "lamp", kitchen),
        Event(second_tv, "lamp", living_room),
        Event(second_tv + timedelta(minutes=1), "glow", second_scene),
        Event(tv_off, "glow", ""),
        Event(night_end, "lamp", ""),
    ]
    if rng.random() < 0.6:
        evening_events.append(Event(kitchen_break + timedelta(minutes=2), "sound", rng.choice(("dishwasher", "cabinet"))))
    if rng.random() < 0.38:
        evening_events.append(Event(night_end - timedelta(minutes=2), "sound", "footsteps"))

    events = sorted((*morning_events, *evening_events), key=lambda event: event.at)
    return DayPlan(day, sunset, tuple(events))
