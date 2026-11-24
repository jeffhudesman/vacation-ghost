"""Output adapters for simulation and a local Raspberry Pi installation."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LOG = logging.getLogger(__name__)
MAX_VOLUME = 0.20
MAX_SOUND_SECONDS = 30
SOUND_CUES = ("cabinet", "dishwasher", "footsteps", "kettle", "tv")
RGB = tuple[int, int, int]


class SimulationOutputs:
    """Keep output state in memory for development without attached hardware."""

    def __init__(self) -> None:
        self.lamps: dict[str, bool] = {}
        self.glow: RGB = (0, 0, 0)
        self.active_sound: str | None = None

    def set_lamp(self, room: str, on: bool) -> None:
        if self.lamps.get(room) != on:
            LOG.info("Lamp in %s %s", room, "on" if on else "off")
        self.lamps[room] = on

    def set_glow(self, rgb: RGB) -> None:
        if rgb != self.glow:
            LOG.debug("TV glow RGB %s", rgb)
        self.glow = rgb

    def play_sound(self, cue: str) -> None:
        self.active_sound = cue
        LOG.info("Sound cue %s", cue)

    def stop_sound(self) -> None:
        self.active_sound = None

    def poll_audio(self) -> None:
        pass

    def close(self) -> None:
        self.stop_sound()


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _switch_origin(value: str) -> str:
    url = urlsplit(value if "://" in value else f"http://{value}")
    try:
        address = ipaddress.ip_address(url.hostname or "")
        port = url.port
    except ValueError as exc:
        raise ValueError(
            "Plug addresses must contain a private IP literal and optional port"
        ) from exc
    if (
        url.scheme not in {"http", "https"}
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or not address.is_private
        or address.is_unspecified
        or address.is_multicast
        or port == 0
    ):
        raise ValueError("Plug addresses must be private HTTP or HTTPS origins")
    host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if url.scheme == "https" else 80
    authority = f"{host}:{port}" if port not in {None, default_port} else host
    return f"{url.scheme}://{authority}"


def _load_glow_driver():
    try:
        from rpi_ws281x import Color, PixelStrip
    except ImportError as exc:
        raise RuntimeError(
            "Install rpi_ws281x on the Raspberry Pi to drive the LED strip"
        ) from exc
    return Color, PixelStrip


def _read_sound(path: Path) -> tuple[int, int, bytes]:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            frames = source.getnframes()
            rate = source.getframerate()
            if (
                channels not in {1, 2}
                or source.getsampwidth() != 2
                or not 8_000 <= rate <= 48_000
                or not 0 < frames <= rate * MAX_SOUND_SECONDS
            ):
                raise ValueError(
                    f"{path.name} must be a 16-bit mono/stereo PCM WAV "
                    "of at most 30 seconds at 8–48 kHz"
                )
            data = source.readframes(frames)
            if len(data) != frames * channels * 2:
                raise ValueError(f"{path.name} is truncated")
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"{path.name} is not a readable PCM WAV") from exc
    return channels, rate, data


class PiOutputs:
    """Drive Shelly Gen2+ switch channel 0, a WS2812-style strip, and ALSA.

    Plug values are private IP literals or HTTP(S) origins for such IPs.
    The plug API must accept local requests without authentication.
    Set led_count or volume to zero to disable the corresponding peripheral.
    """

    def __init__(
        self,
        plugs: dict[str, str],
        led_count: int = 0,
        led_pin: int = 18,
        sounds_dir: Path | None = None,
        volume: float = 0,
    ) -> None:
        if not 0 <= volume <= MAX_VOLUME:
            raise ValueError(f"Volume must be between 0 and {MAX_VOLUME}")
        if led_count < 0:
            raise ValueError("LED count must be nonnegative")
        self._origins = {room: _switch_origin(value) for room, value in plugs.items()}
        if len(set(self._origins.values())) != len(self._origins):
            raise ValueError("Each room needs its own plug")
        self._http = build_opener(ProxyHandler({}), _RejectRedirects())
        self._sounds_dir = sounds_dir
        self._volume = volume
        self._player: subprocess.Popen[bytes] | None = None
        self._led_count = led_count
        self._led_pin = led_pin
        self._color = None
        self._strip = None

    def _switch_call(self, room: str, operation: str, data: bytes | None = None) -> dict:
        request = Request(
            f"{self._origins[room]}/rpc/{operation}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with self._http.open(request, timeout=2) as response:
                result = json.loads(response.read(1024))
        except (OSError, HTTPException, ValueError) as exc:
            raise RuntimeError(f"Shelly {operation} failed for {room}: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"Shelly {operation} returned an invalid response for {room}")
        return result

    def lamp_status(self, room: str) -> bool:
        result = self._switch_call(room, "Switch.GetStatus?id=0")
        output = result.get("output")
        if not isinstance(output, bool):
            raise RuntimeError(f"Shelly did not report a switch state for {room}")
        return output

    def set_lamp(self, room: str, on: bool) -> None:
        result = self._switch_call(
            room, "Switch.Set", json.dumps({"id": 0, "on": on}).encode("utf-8")
        )
        if not isinstance(result.get("was_on"), bool):
            raise RuntimeError(f"Shelly switch did not acknowledge the request for {room}")
        if self.lamp_status(room) != on:
            raise RuntimeError(f"Shelly switch in {room} did not turn {'on' if on else 'off'}")

    def check_glow(self) -> bool:
        if not self._led_count:
            return False
        _load_glow_driver()
        return True

    def set_glow(self, rgb: RGB) -> None:
        if not self._led_count:
            return
        if self._strip is None:
            self._color, pixel_strip = _load_glow_driver()
            strip = pixel_strip(self._led_count, self._led_pin)
            strip.begin()
            self._strip = strip
        color = self._color(*rgb)
        for index in range(self._strip.numPixels()):
            self._strip.setPixelColor(index, color)
        self._strip.show()

    def _audio_settings(self) -> tuple[Path, str]:
        if self._sounds_dir is None:
            raise ValueError("Set hardware.sounds_dir or set hardware.volume to zero")
        aplay = shutil.which("aplay")
        if aplay is None:
            raise RuntimeError("Install alsa-utils to play sounds with aplay")
        return self._sounds_dir, aplay

    def check_audio(self) -> bool:
        if not self._volume:
            return False
        sounds_dir, _ = self._audio_settings()
        for cue in SOUND_CUES:
            _read_sound(sounds_dir / f"{cue}.wav")
        return True

    def play_sound(self, cue: str) -> None:
        if not self._volume:
            return
        if not re.fullmatch(r"[a-z0-9_-]+", cue):
            raise ValueError("Invalid sound cue name")
        sounds_dir, aplay = self._audio_settings()
        channels, rate, data = _read_sound(sounds_dir / f"{cue}.wav")

        samples = array("h", data)
        if sys.byteorder != "little":
            samples.byteswap()
        attenuated = array("h", (int(sample * self._volume) for sample in samples))
        if sys.byteorder != "little":
            attenuated.byteswap()

        with tempfile.TemporaryFile() as audio:
            with wave.open(audio, "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(2)
                target.setframerate(rate)
                target.writeframes(attenuated.tobytes())
            audio.seek(0)
            self.stop_sound()
            self._player = subprocess.Popen([aplay, "--quiet"], stdin=audio)

    def poll_audio(self) -> None:
        if self._player is None:
            return
        returncode = self._player.poll()
        if returncode is None:
            return
        self._player = None
        if returncode:
            raise RuntimeError(f"aplay exited with status {returncode}")

    def stop_sound(self) -> None:
        if self._player is None:
            return
        if self._player.poll() is None:
            self._player.terminate()
            try:
                self._player.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._player.kill()
                self._player.wait()
        elif self._player.returncode:
            LOG.warning("aplay exited with status %s", self._player.returncode)
        self._player = None

    def close(self) -> None:
        self.stop_sound()
