"""Prepare a private Markdown record for an on-site two-lamp acceptance test."""

import argparse
import json
import os
from pathlib import Path


CHECKS = (
    "With the Pi disconnected, verify each plug’s own auto-off timer. Record each delay.",
    "Restore power to each plug. Verify that neither lamp turns on by itself.",
    "Run doctor and rehearse. Observe each lamp taking its turn and both lamps turning off.",
    "Commission a future test trip. Cold-boot the Pi and verify the clock and idle service.",
    "Reboot during an active test trip. Observe the lamps and the schedule after reconnecting.",
    "Disconnect the network with a lamp on. Time its auto-off and verify off-only recovery.",
    "Disarm the test trip while active. Verify shutdown, then reboot and check it stays disarmed.",
    "Let another test trip finish on schedule. Verify shutdown and that reboot keeps it finished.",
    "Compare the morning note and final report with the observed lamps.",
)


def status_excerpt(directory: Path) -> list[str]:
    """Include only fixed status fields from actual hardware-mode reports for one trip."""
    summaries = []
    owner = None
    for filename in ("latest.json", "trip.json"):
        path = directory / filename
        if filename == "trip.json" and not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("dry_run") is not False:
            raise ValueError(f"{filename} must be a hardware-mode report")
        trip = data.get("trip_id")
        if not isinstance(trip, str) or not trip or (owner and trip != owner):
            raise ValueError("reports must identify the same trip")
        owner = trip
        fields = []
        for key in ("active", "disarmed", "completed", "ended_early", "shutdown_unconfirmed",
                    "relay_off_verified", "led_black_frame_sent"):
            if key in data:
                value = data[key]
                if value is not None and type(value) is not bool:
                    raise ValueError(f"{filename} has an invalid {key}")
                fields.append(f"{key}={json.dumps(value)}")
        count = data.get("device_faults")
        if type(count) is not int or count < 0:
            raise ValueError(f"{filename} has an invalid device_faults count")
        fields.append(f"device_faults={count}")
        summaries.append(f"- `{filename}` — " + ", ".join(fields))
    return summaries


def rehearsal_excerpt(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("outcome") not in ("completed", "interrupted", "failed"):
        raise ValueError("invalid rehearsal outcome")
    rooms = data.get("lamps_confirmed_on")
    if not isinstance(rooms, list) or not all(isinstance(room, str) for room in rooms):
        raise ValueError("invalid rehearsed lamp list")
    count = len(set(rooms))
    noun = "lamp" if count == 1 else "lamps"
    return f"- `rehearsal.json` — {data['outcome']}, {count} distinct {noun} confirmed on"


def render(directory: Path | None = None, rehearsal: Path | None = None) -> str:
    evidence = status_excerpt(directory) if directory else []
    if rehearsal:
        evidence.append(rehearsal_excerpt(rehearsal))
    if not evidence:
        evidence.append("No controller reports attached.")
    rows = [
        "# Vacation Ghost physical acceptance record", "",
        "Overall result — Pending on-site observations.", "",
        "| Bench detail | Value |", "| --- | --- |",
        "| Test date and release commit | Pending |",
        "| Pi model, OS image, kernel and Python | Pending |",
        "| Service profile | Lights only |",
        "| First and second plug models and firmware | Pending |",
        "| Lamp types and configured auto-off delays | Pending |", "",
        "## On-site observations", "",
        "For each row, enter pass, fail, or untested and describe what you saw.", "",
        "| Check | Result | What you observed |", "| --- | --- | --- |",
        *(f"| {step} | Untested | |" for step in CHECKS), "",
        "## Controller reports", "", *evidence, "",
        "Record optional LED blackout and audio playback in a separate run if fitted.", "",
        "Before sharing, remove household addresses, trip dates and device identifiers.", "",
    ]
    return "\n".join(rows)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new Markdown file")
    parser.add_argument("--state-dir", type=Path, help="attach selected status fields from a real trip")
    parser.add_argument("--rehearsal", type=Path, help="attach the result of a development rehearsal.json")
    args = parser.parse_args(argv)
    try:
        document = render(args.state_dir, args.rehearsal)
        with open(args.output, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(document)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Could not prepare bench record: {exc}\n")
    print(f"Bench record ready at {args.output}")


if __name__ == "__main__":
    main()
