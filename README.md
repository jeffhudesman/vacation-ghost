# Vacation Ghost

Vacation Ghost makes an empty house look and sound lived-in while you’re away. A Raspberry Pi runs a local, time-zone-aware routine for two lamps, an LED strip that casts a TV-like glow, and quiet household sounds. Evening activity follows sunset, moves between rooms one lamp at a time, and winds down at a bedtime that varies each night. The Pi writes morning notes and a trip summary locally.

The first version takes manually entered trip dates. It uses no microphone or camera and collects no audio. The schedule and reports stay on the device. Other local applications can read the JSON reports.

## Try it without hardware

Use Python 3.11 or later and run these commands from the checkout. The simulator uses Python’s standard library and your system’s time-zone database.

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

For a foreground trial, use Python 3.11 or later on Raspberry Pi OS. Install `python3-venv`, the LED driver if using glow, and `alsa-utils` if using sound. Make USB audio the default ALSA output for the account running Vacation Ghost. Use `sudo` when the Pi needs root for PWM/DMA access.

```sh
python3 -m venv .venv
device_dir="$HOME/.local/share/vacation-ghost"
(
  umask 077
  install -d -m 700 "$device_dir"
  cp -n examples/vacation.example.toml "$device_dir/vacation.toml"
)
```

For glow, run `.venv/bin/python -m pip install rpi_ws281x`. For sound, run `.venv/bin/python tools/generate_sounds.py "$device_dir/sounds"` to make five original cues. Edit the local TOML before starting the device.

| Section | Settings |
| --- | --- |
| `[trip]` | Set `start` and `end` to ISO 8601 timestamps with UTC offsets. Use an IANA `timezone` such as `Etc/UTC` for the routine and the reports. |
| `[routine]` | List the kitchen lamp first and the living room lamp second in `rooms`. Set `sunset` from `15:00` to `22:00` and `bedtime` from `18:00` to `23:55` in local time. If you supply `latitude` and `longitude`, the routine calculates sunset and uses `sunset` as a fallback. An optional `seed` controls the repeatable variation from day to day. |
| `[hardware]` | For glow, set `led_count` and the BCM `led_pin`. For sound, set `sounds_dir` (relative to the config or absolute) and `volume` up to `0.20`, starting low. For lights only, use `led_count = 0` and `volume = 0`. |
| `[hardware.plugs]` | Map both room names to their plug addresses, for example `http://192.168.1.40` and `http://192.168.1.41`. Use literal private IPs. |

Run `doctor` to check the configuration and contact both plugs without switching them. While you’re home, run `rehearse` to switch the lamps and try the enabled glow and sound, then shut everything off. Keep the scheduled service stopped while you rehearse. Choose a fresh state directory for each real trip and reuse it when restarting the same trip.

```sh
sudo "$PWD/.venv/bin/python" -m vacation_ghost --config "$device_dir/vacation.toml" doctor
rehearsal_dir=$(sudo mktemp -d "$device_dir/rehearsal.XXXXXX")
sudo "$PWD/.venv/bin/python" -m vacation_ghost \
  --config "$device_dir/vacation.toml" rehearse --state-dir "$rehearsal_dir"
sudo "$PWD/.venv/bin/python" -m vacation_ghost \
  --config "$device_dir/vacation.toml" run --state-dir "$device_dir/trips/my-trip"
```

The run shuts the outputs off at the scheduled end. To end it early, run `disarm` with the same config and state directory. Wait for the runner to finish and check `latest.json` for `shutdown_unconfirmed` before reusing the devices. The [service guide](docs/service.md#stop-or-change-trips) covers retrying shutdown and preparing a new trip.

## Read reports locally

| File in the state directory | Purpose |
| --- | --- |
| `latest.json` | Current status, including `active`, `lamp_room`, the `updated_at` heartbeat, `shutdown_unconfirmed`, `faults_by_device`, and `device_faults`. The process replaces this file atomically. |
| `daily/YYYY-MM-DD.json` | Note due at 8 AM local time, named for its `delivery_date`. The `night_of` field is the previous evening’s date. |
| `trip.json` | End-of-trip summary in `message`. |

The hardware reports reflect the plugs’ acknowledgments of the controller’s switch requests. An audio or glow fault is reported in the device status while the lamps can continue running.

A local notification app can read the file for the current delivery date, use the stable `id` to send `message` once, and skip reports with `dry_run` set to `true`. It can read `trip.json` the same way. Both reports include a `schema_version` and `trip_id`. Report files are private to the runner’s account by default. If the runner uses `sudo`, configure explicit local read access for the notification app.

See [Contributing](CONTRIBUTING.md) for the simulator workflow and test commands.

## Build and contribute

Run the simulator from the checkout with Python 3.11+, or install the Linux CLI with `python3 -m pip install .` in a virtual environment. The source checkout also contains the cue generator and service files.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. Vacation Ghost is available under the [MIT License](LICENSE).
