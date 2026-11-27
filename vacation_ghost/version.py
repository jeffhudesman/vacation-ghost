"""Describe the installed release, wheel, source archive, or development checkout."""

import argparse
import json
import re
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path


# git archive expands this token. It survives the sdist-to-wheel build.
_ARCHIVE_COMMIT = "$Format:%H$"


def _commit(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def build_description() -> str:
    package = Path(__file__).resolve().parent
    receipt = package / "BUILD.json"
    if receipt.is_file():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("version"), str) and _commit(data.get("commit")):
                return f"Vacation Ghost {data['version']} (commit {data['commit']})"
        except (OSError, ValueError):
            pass
        return "Vacation Ghost (invalid installed BUILD.json)"

    root = package.parent
    project_file = root / "pyproject.toml"
    version = None
    modified = False
    commit = _ARCHIVE_COMMIT if _commit(_ARCHIVE_COMMIT) else None
    if project_file.is_file():
        with project_file.open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
        if project.get("name") == "vacation-ghost":
            version = project.get("version")
            if (root / ".git").exists():
                try:
                    output = subprocess.check_output(
                        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                        stderr=subprocess.DEVNULL, text=True, timeout=3,
                    ).strip()
                    if _commit(output):
                        commit = output
                        modified = bool(subprocess.check_output(
                            ["git", "-C", str(root), "status", "--porcelain", "--", "vacation_ghost", "pyproject.toml"],
                            stderr=subprocess.DEVNULL, text=True, timeout=3,
                        ).strip())
                except (OSError, subprocess.SubprocessError):
                    pass
    if version is None:
        try:
            version = metadata.version("vacation-ghost")
        except metadata.PackageNotFoundError:
            version = "unknown version"
    suffix = ", modified checkout" if modified else ""
    return f"Vacation Ghost {version}" + (f" (commit {commit}{suffix})" if commit else "")


class VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        print(build_description())
        parser.exit()
