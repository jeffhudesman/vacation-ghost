"""Serialize hardware access and persist recovery and disarm requests."""

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


DEVICE_DIR = Path("/var/lib/vacation-ghost/device")


class DeviceBusy(RuntimeError):
    pass


class InvalidState(ValueError):
    """A saved state record or directory cannot be safely used."""


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not path.parent.is_dir():
            private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_directory(path.parent)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise InvalidState(f"state directory must be owned by the controller without group or public writes: {path}")


def hardware_directory(path: Path) -> None:
    """Create hardware state beneath directories writable only by root or the controller."""
    controller_uid = os.geteuid()
    path = path.absolute()
    if ".." in path.parts:
        raise InvalidState(f"hardware state path cannot contain '..': {path}")
    for directory in (*reversed(path.parents), path):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory.parent)
            info = directory.lstat()
        owners = {controller_uid} if directory == path else {0, controller_uid}
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
            raise InvalidState(f"hardware state requires trusted ownership without group or public writes: {directory}")


@contextmanager
def lock_file(path: Path, *, blocking: bool = False):
    with open(path, "a+b", opener=_private_opener) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise DeviceBusy(f"another Vacation Ghost process holds {path}") from exc
        yield


def _private_opener(path: str, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _validate_state_file(path, os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate_state_file(path: str | Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise InvalidState(f"state file must be a regular file: {path}")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise InvalidState(f"state file must be owned by the controller without group or public writes: {path}")


def read_record(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8", opener=_private_opener) as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidState(f"state file must contain a valid JSON object: {path}") from exc
    if not isinstance(record, dict):
        raise InvalidState(f"state file must contain a JSON object: {path}")
    return record


def write_record(path: Path, record: dict) -> None:
    try:
        _validate_state_file(path, path.lstat())
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def remove_record(path: Path) -> None:
    path.unlink(missing_ok=True)
    sync_directory(path.parent)


class OutputReservation:
    """An outstanding shutdown must be recovered by its original state directory."""

    def __init__(self, state_dir: Path, device_dir: Path | None = None, *, hardware_id: str = ""):
        if device_dir is not None and (not isinstance(hardware_id, str) or not hardware_id):
            raise ValueError("a hardware identity is required for a modern output reservation")
        self.state_dir = state_dir.resolve()
        self.legacy = self.state_dir / ".outputs-active"
        self.path = device_dir / "outputs.json" if device_dir is not None else self.legacy
        self.hardware_id = hardware_id
        self.glow_pending = self._legacy_exists()
        if self.path != self.legacy:
            try:
                record = read_record(self.path)
                if record is not None:
                    self._restore_modern(record)
            except InvalidState as exc:
                raise InvalidState(
                    f"Cannot use pending shutdown record {self.path}. Switch the affected outputs off manually "
                    f"and preserve the reservation for diagnosis. {exc}"
                ) from exc

    def _legacy_exists(self) -> bool:
        # The original marker was only an empty file, so its contents are immaterial.
        try:
            with open(self.legacy, "rb", opener=_private_opener):
                return True
        except FileNotFoundError:
            return False

    def _restore_modern(self, record: dict) -> None:
        owner = record.get("state_dir")
        hardware = record.get("hardware_id")
        glow_pending = record.get("glow_pending")
        if not isinstance(owner, str) or not Path(owner).is_absolute():
            raise InvalidState("pending shutdown record has an invalid state directory")
        if not isinstance(hardware, str) or not hardware or not isinstance(glow_pending, bool):
            raise InvalidState("pending shutdown record has missing or invalid hardware metadata")
        if owner != str(self.state_dir):
            raise RuntimeError(f"shutdown is still pending for {owner}; recover it before using another state directory")
        if hardware != self.hardware_id:
            raise ValueError("restore the original plug and LED configuration before retrying shutdown")
        self.glow_pending = self.glow_pending or glow_pending

    @property
    def engaged(self) -> bool:
        return self.path.exists() or self.legacy.exists()

    def engage(self) -> None:
        write_record(self.path, {
            "state_dir": str(self.state_dir),
            "hardware_id": self.hardware_id,
            "glow_pending": self.glow_pending,
        })

    def before_glow(self) -> None:
        if not self.glow_pending:
            self.glow_pending = True
            self.engage()

    def clear(self) -> None:
        if self.path != self.legacy and self.legacy.exists():
            remove_record(self.legacy)
        remove_record(self.path)
        self.glow_pending = False
