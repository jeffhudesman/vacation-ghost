# Vacation Ghost

Vacation Ghost makes an empty house look and sound lived-in while you’re away. A Raspberry Pi runs a local, time-zone-aware routine for two lamps, an LED strip that casts a TV-like glow, and quiet household sounds. Evening activity follows sunset, moves between rooms one lamp at a time, and winds down at a bedtime that varies each night. The Pi writes morning notes and a trip summary locally.

The first version takes manually entered trip dates. It uses no microphone or camera and collects no audio. The schedule and reports stay on the device. Other local applications can read the JSON reports.

## Try it without hardware

On Linux with Python 3.11 or later, run these commands from the checkout. The simulator uses Python’s standard library and your system’s time-zone database.

```sh
demo_dir=$(mktemp -d /tmp/vacation-ghost-demo.XXXXXX)
python3 -m vacation_ghost --config examples/vacation.example.toml simulate \
  --at 2030-06-11T09:00:00Z --state-dir "$demo_dir"
cat "$demo_dir/daily/2030-06-11.json"
```

The example uses a fictional June 10–13, 2030 trip in UTC. The simulation replays the trip up to `--at` and generates any reports due by that time. Omit `--at` to replay the whole trip and generate the final summary. Use a fresh temporary directory for each replay and keep device state in its own directory.

```sh
python3 -m unittest discover -s tests -v
```

## Set up the Raspberry Pi

Use a Raspberry Pi and two Shelly Gen2 or Gen3 plugs on the local network. The LED strip and USB sound are optional. See the [parts list, wiring diagram, and physical checklist](docs/hardware.md) and the [systemd installation guide](docs/service.md) for a Pi that should resume after a reboot.

The service guide covers installing a verified release, checking both plugs with `doctor`, and trying the outputs with `rehearse`. Start with the lights-only Pi example and add glow or sound after the lamp checks pass. [Service guide](docs/service.md) · [Lights-only example](examples/vacation.lights-only.toml)

| Section | Settings |
| --- | --- |
| `[trip]` | Set `start` and `end` to ISO 8601 timestamps with UTC offsets. Use an IANA `timezone` such as `Etc/UTC` for the routine and the reports. |
| `[routine]` | List the kitchen lamp first and the living room lamp second in `rooms`. Set `sunset` from `15:00` to `22:00` and `bedtime` from `18:00` to `23:55` in local time. If you supply `latitude` and `longitude`, the routine calculates sunset and uses `sunset` as a fallback. An optional `seed` controls the repeatable variation from day to day. |
| `[hardware]` | For glow, set `led_count` and the BCM `led_pin`. For sound, set `sounds_dir` (relative to the config or absolute) and `volume` up to `0.20`, starting low. For lights only, use `led_count = 0` and `volume = 0`. |
| `[hardware.plugs]` | Map both room names to their plug addresses, for example `http://192.168.1.40` and `http://192.168.1.41`. Use RFC 1918 IPv4 or unique-local IPv6 literals. |

The run shuts the outputs off at the scheduled end. It can end early with `disarm`. Use `recover` for an unconfirmed shutdown. It only tries to turn devices off. Keep the original plug settings and state directory until shutdown is confirmed. [Service operator guide](docs/service.md#stop-recover-or-change-trips)

## Read reports locally

| File in the state directory | Purpose |
| --- | --- |
| `latest.json` | Current status, including `active`, `lamp_room`, the `updated_at` heartbeat, `shutdown_unconfirmed`, `faults_by_device`, and `device_faults`. The process replaces this file atomically. |
| `daily/YYYY-MM-DD.json` | Note due at 8 AM local time, named for its `delivery_date`. The `night_of` field is the previous evening’s date. |
| `trip.json` | End-of-trip summary in `message`. |

The controller checks the active lamp at each 15-second heartbeat. If the plug has switched off unexpectedly, it records a fault and restores the scene if the schedule still calls for that lamp. An audio or glow fault is reported in the device status while the lamps can continue running.

A local notification app can read the file for the current delivery date, use the stable `id` to send `message` once, and skip reports with `dry_run` set to `true`. It can read `trip.json` the same way. Both reports include a `schema_version` and `trip_id`. Export only the needed reports through a [read-only handoff](docs/service.md#share-reports-locally) and keep the controller’s state directory private.

If an existing published report is malformed, the controller saves a `.corrupt-*` copy and regenerates the report from trip activity using the same ID.

See [Contributing](CONTRIBUTING.md) for the simulator workflow and test commands.

## Build and contribute

Run the simulator from the checkout with Python 3.11+, or install the Linux CLI with `python3 -m pip install .` in a virtual environment. The source checkout also contains the cue generator and service files.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. Vacation Ghost is available under the [MIT License](LICENSE).
