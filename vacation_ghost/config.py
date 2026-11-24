"""Read and validate the local configuration."""

import math
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Hardware:
    plugs: dict[str, str]
    led_count: int
    led_pin: int
    sounds_dir: Path | None
    volume: float


@dataclass(frozen=True)
class Config:
    start: datetime
    end: datetime
    timezone: ZoneInfo
    rooms: tuple[str, str]
    sunset: time
    bedtime: time
    latitude: float | None
    longitude: float | None
    seed: str
    hardware: Hardware | None


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamps must include an explicit UTC offset")
    return value


def _clock(value: str, lower: time, upper: time) -> time:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("routine times must use HH:MM")
    clock = time.fromisoformat(value)
    if not lower <= clock <= upper:
        raise ValueError(f"routine time {value} must be between {lower:%H:%M} and {upper:%H:%M}")
    return clock


def _bounded_number(value: object, lower: float, upper: float, name: str) -> float:
    if type(value) not in (int, float) or not lower <= value <= upper or not math.isfinite(value):
        raise ValueError(f"{name} must be between {lower:g} and {upper:g}")
    return float(value)


def _hardware(table: dict, rooms: tuple[str, str], directory: Path) -> Hardware:
    plugs = table["plugs"]
    if not isinstance(plugs, dict) or set(plugs) != set(rooms) or any(
        not isinstance(url, str) or not url.strip() for url in plugs.values()
    ):
        raise ValueError("hardware.plugs must have one address for each routine room")
    if len(set(plugs.values())) != 2:
        raise ValueError("hardware.plugs must use two distinct plugs")
    led_count = table.get("led_count", 0)
    led_pin = table.get("led_pin", 18)
    if type(led_count) is not int or led_count < 0:
        raise ValueError("hardware.led_count must be a nonnegative integer")
    if type(led_pin) is not int or led_pin < 0:
        raise ValueError("hardware.led_pin must be a nonnegative GPIO number")
    sounds_dir = table.get("sounds_dir")
    volume = _bounded_number(
        table.get("volume", 0.08 if sounds_dir is not None else 0),
        0, 0.20, "hardware.volume",
    )
    return Hardware(
        plugs=plugs,
        led_count=led_count,
        led_pin=led_pin,
        sounds_dir=directory / Path(sounds_dir).expanduser() if sounds_dir is not None else None,
        volume=volume,
    )


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        trip = document["trip"]
        routine = document["routine"]
        timezone = ZoneInfo(trip["timezone"])
        start = parse_timestamp(trip["start"]).astimezone(timezone)
        end = parse_timestamp(trip["end"]).astimezone(timezone)
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise ValueError("trip.end must be later than trip.start")
        names = routine["rooms"]
        if (
            not isinstance(names, list)
            or len(names) != 2
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or names[0] == names[1]
        ):
            raise ValueError("routine.rooms must contain two distinct room names")
        rooms = (names[0], names[1])
        latitude = routine.get("latitude")
        longitude = routine.get("longitude")
        if (latitude is None) != (longitude is None):
            raise ValueError("set both routine.latitude and routine.longitude to use local sunset")
        if latitude is not None:
            latitude = _bounded_number(latitude, -90, 90, "routine.latitude")
            longitude = _bounded_number(longitude, -180, 180, "routine.longitude")
        seed = routine.get("seed", "vacation-ghost")
        if not isinstance(seed, str):
            raise ValueError("routine.seed must be a string")
        return Config(
            start=start,
            end=end,
            timezone=timezone,
            rooms=rooms,
            sunset=_clock(routine["sunset"], time(15), time(22)),
            bedtime=_clock(routine["bedtime"], time(18), time(23, 55)),
            latitude=latitude,
            longitude=longitude,
            seed=seed,
            hardware=_hardware(document["hardware"], rooms, path.parent) if "hardware" in document else None,
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
