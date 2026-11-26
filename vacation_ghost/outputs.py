"""Output adapters for simulation and a local Raspberry Pi installation."""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import wave
from array import array
from collections.abc import Callable
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from .config import DEFAULT_LED_BACKEND, LED_CHANNELS, resolve_led_pin
from .spi_glow import SpiGlow

LOG = logging.getLogger(__name__)
MAX_VOLUME = 0.20
MAX_SOUND_SECONDS = 30
# A 30-second stereo cue at 48 kHz uses under 6 MiB before WAV metadata.
MAX_SOUND_BYTES = 8 * 1024 * 1024
SOUND_STOP_TIMEOUT = 1
SOUND_CUES = ("cabinet", "dishwasher", "footsteps", "kettle", "tv")
RGB = tuple[int, int, int]
RPC_TIMEOUT = 2
MAX_RPC_BYTES = 1024
PLUG_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


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

    def lamp_status(self, room: str) -> bool:
        return self.lamps.get(room, False)

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


def _switch_origin(value: str) -> str:
    if "://" not in value:
        if value.count(":") > 1 and not value.startswith("["):
            value = f"[{value}]"
        value = f"http://{value}"
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname or "")
        port = url.port
    except ValueError as exc:
        raise ValueError(
            "Plug addresses must use a private IP literal. Put IPv6 addresses in brackets in URLs or with a port."
        ) from exc
    if (
        url.scheme not in {"http", "https"}
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or not any(address in network for network in PLUG_NETWORKS)
        or getattr(address, "scope_id", None) is not None
        or port == 0
    ):
        raise ValueError("Plug addresses must be private HTTP or HTTPS origins")
    host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if url.scheme == "https" else 80
    authority = f"{host}:{port}" if port not in {None, default_port} else host
    return f"{url.scheme}://{authority}"


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("RPC deadline exceeded")
    return remaining


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._socket = sock
        self._deadline = deadline
        # The socket file stays open while HTTPConnection handles Connection: close.
        self._stream = sock.makefile("rb", buffering=0)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int | None:
        self._socket.settimeout(_remaining(self._deadline))
        return self._stream.readinto(buffer)

    def close(self) -> None:
        self._stream.close()
        super().close()


class _DeadlineSocket:
    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._socket = sock
        self._deadline = deadline

    def sendall(self, data: bytes) -> None:
        self._socket.settimeout(_remaining(self._deadline))
        self._socket.sendall(data)

    def makefile(self, mode: str) -> io.BufferedReader:
        return io.BufferedReader(_DeadlineReader(self._socket, self._deadline))

    def close(self) -> None:
        self._socket.close()


def _rpc_request(
    origin: str, operation: str, data: bytes | None, *, can_send: Callable[[], bool] | None = None
) -> bytes | None:
    deadline = monotonic() + RPC_TIMEOUT
    url = urlsplit(origin)
    host = url.hostname
    tls = url.scheme == "https"
    port = url.port or (443 if tls else 80)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    connection = HTTPConnection(url.netloc)
    sock = socket.socket(family)
    try:
        sock.settimeout(_remaining(deadline))
        sock.connect((host, port))
        if tls:
            context = ssl.create_default_context()
            sock.settimeout(_remaining(deadline))
            sock = context.wrap_socket(sock, server_hostname=host)
        connection.sock = _DeadlineSocket(sock, deadline)
        headers = {"Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if can_send is not None and not can_send():
            return None
        connection.request("GET" if data is None else "POST", f"/rpc/{operation}", data, headers)
        with connection.getresponse() as response:
            if not 200 <= response.status < 300:
                raise HTTPException(f"HTTP status {response.status}")
            body = response.read(MAX_RPC_BYTES + 1)
            _remaining(deadline)
            if len(body) > MAX_RPC_BYTES:
                raise HTTPException("RPC response exceeds the size limit")
            return body
    finally:
        connection.close()
        sock.close()


def _load_glow_driver():
    try:
        from rpi_ws281x import Color, PixelStrip
    except ImportError as exc:
        raise RuntimeError(
            "Install rpi_ws281x on the Raspberry Pi to drive the LED strip"
        ) from exc
    return Color, PixelStrip


def _open_sound(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def _read_sound(path: Path) -> tuple[int, int, bytes]:
    try:
        with open(path, "rb", opener=_open_sound) as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path.name} must be a regular file")
            if info.st_size > MAX_SOUND_BYTES:
                raise ValueError(f"{path.name} exceeds the 8 MiB cue size limit")
            with wave.open(file, "rb") as source:
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
        led_pin: int | None = None,
        sounds_dir: Path | None = None,
        volume: float = 0,
        *,
        led_backend: str = DEFAULT_LED_BACKEND,
    ) -> None:
        if not 0 <= volume <= MAX_VOLUME:
            raise ValueError(f"Volume must be between 0 and {MAX_VOLUME}")
        self._led_pin = resolve_led_pin(led_backend, led_pin, led_count)
        self._origins = {room: _switch_origin(value) for room, value in plugs.items()}
        if len(set(self._origins.values())) != len(self._origins):
            raise ValueError("Each room needs its own plug")
        self._plug_identities: dict[str, str] | None = None
        self._sounds_dir = sounds_dir
        self._volume = volume
        self._player: subprocess.Popen[bytes] | None = None
        self._led_count = led_count
        self._spi_glow = SpiGlow(led_count) if led_count and led_backend == "spi" else None
        self._color = None
        self._strip = None

    def bind_plug_identities(self, identities: dict[str, str]) -> None:
        """Require reads and writes to reach the recorded device for each room."""
        self._plug_identities = identities.copy()

    def _verify_plug_identity(self, room: str) -> None:
        if self._plug_identities is not None and self.plug_identity(room) != self._plug_identities.get(room):
            raise RuntimeError(f"Shelly plug identity changed for {room}. Check the configured address before retrying.")

    def _switch_call(
        self, room: str, operation: str, data: bytes | None = None,
        *, can_send: Callable[[], bool] | None = None,
    ) -> dict | None:
        try:
            origin = self._origins[room]
            if can_send is None:
                response = _rpc_request(origin, operation, data)
            else:
                response = _rpc_request(origin, operation, data, can_send=can_send)
            if response is None:
                return None
            result = json.loads(response)
        except (OSError, HTTPException, ValueError) as exc:
            raise RuntimeError(f"Shelly {operation} failed for {room}: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"Shelly {operation} returned an invalid response for {room}")
        return result

    def lamp_status(self, room: str) -> bool:
        self._verify_plug_identity(room)
        result = self._switch_call(room, "Switch.GetStatus?id=0")
        output = result.get("output")
        if not isinstance(output, bool):
            raise RuntimeError(f"Shelly did not report a switch state for {room}")
        return output

    def plug_identity(self, room: str) -> str:
        """Read a stable identity independent of the address used to reach the plug."""
        result = self._switch_call(room, "Shelly.GetDeviceInfo")
        mac = result.get("mac")
        if not isinstance(mac, str) or not re.fullmatch(r"[0-9a-fA-F]{12}", mac):
            raise RuntimeError(f"Shelly did not report a valid device MAC for {room}")
        return mac.lower()

    def set_lamp(self, room: str, on: bool) -> None:
        self._set_lamp(room, on)

    def set_lamp_if(self, room: str, can_start: Callable[[], bool]) -> bool:
        """Recheck the scene after identifying and connecting to the plug."""
        return self._set_lamp(room, True, can_start)

    def _set_lamp(self, room: str, on: bool, can_start: Callable[[], bool] | None = None) -> bool:
        self._verify_plug_identity(room)
        result = self._switch_call(
            room, "Switch.Set", json.dumps({"id": 0, "on": on}).encode("utf-8"), can_send=can_start
        )
        if result is None:
            return False
        if not isinstance(result.get("was_on"), bool):
            raise RuntimeError(f"Shelly switch did not acknowledge the request for {room}")
        if self.lamp_status(room) != on:
            raise RuntimeError(f"Shelly switch in {room} did not turn {'on' if on else 'off'}")
        return True

    def check_glow(self) -> bool:
        if not self._led_count:
            return False
        if self._spi_glow is None:
            _load_glow_driver()
        else:
            self._spi_glow.check()
        return True

    def set_glow(self, rgb: RGB) -> None:
        if not self._led_count:
            return
        if self._spi_glow is not None:
            self._spi_glow.show(rgb)
            return
        if self._strip is None:
            self._color, pixel_strip = _load_glow_driver()
            strip = pixel_strip(self._led_count, self._led_pin, channel=LED_CHANNELS[self._led_pin])
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
        self._play_sound(cue)

    def play_sound_if(self, cue: str, can_start: Callable[[], bool]) -> bool:
        """Recheck the cue after preparing the audio and before starting playback."""
        return self._play_sound(cue, can_start)

    def _play_sound(self, cue: str, can_start: Callable[[], bool] | None = None) -> bool:
        if not self._volume:
            return False
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
            if can_start is not None and not can_start():
                return False
            self._player = subprocess.Popen([aplay, "--quiet"], stdin=audio)
        return True

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
                self._player.wait(timeout=SOUND_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._player.kill()
                try:
                    self._player.wait(timeout=SOUND_STOP_TIMEOUT)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("aplay did not stop after being killed") from exc
        elif self._player.returncode:
            LOG.warning("aplay exited with status %s", self._player.returncode)
        self._player = None

    def close(self) -> None:
        try:
            self.stop_sound()
        finally:
            if self._spi_glow is not None:
                self._spi_glow.close()
