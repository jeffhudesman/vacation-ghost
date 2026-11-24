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


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"state directory cannot be a symbolic link: {path}")
    if path.is_dir():
        return
    private_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    sync_directory(path.parent)


def hardware_directory(path: Path) -> None:
    """Create hardware state beneath trusted directories when running as root."""
    if os.geteuid() != 0:
        private_directory(path)
        return
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError(f"privileged state path cannot contain '..': {path}")
    for directory in (*reversed(path.parents), path):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory.parent)
            info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"privileged state requires a root-owned directory without group or public writes: {directory}")


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
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"state file must be a regular file: {path}")
        if os.geteuid() == 0 and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError(f"privileged state file must be root-owned without group or public writes: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_record(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8", opener=_private_opener) as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(record, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return record


def write_record(path: Path, record: dict) -> None:
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
        self.state_dir = state_dir.resolve()
        self.legacy = self.state_dir / ".outputs-active"
        self.path = device_dir / "outputs.json" if device_dir is not None else self.legacy
        self.hardware_id = hardware_id
        self.glow_pending = self.legacy.exists()
        if self.path != self.legacy:
            record = read_record(self.path)
            owner = record.get("state_dir")
            if self.path.exists() and owner != str(self.state_dir):
                if owner is None:
                    raise ValueError(f"{self.path} is missing its owner")
                raise RuntimeError(f"shutdown is still pending for {owner}; recover it before using another state directory")
            if record.get("hardware_id", hardware_id) != hardware_id:
                raise ValueError("restore the original plug and LED configuration before retrying shutdown")
            self.glow_pending = self.glow_pending or record.get("glow_pending", False)

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
