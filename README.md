# Vacation Ghost

Make home look lived in while you’re away. Set your trip dates and Vacation Ghost uses a Raspberry Pi to switch two lamps in turn near sunset, vary bedtime, and save a morning report. The schedule and reports stay on your Pi.

- [Watch an evening · 21 sec](https://vacationghost.com/#sample-evening)
- [Run the Linux simulator](#try-it-without-hardware)
- [Build with two lamps](docs/hardware.md#before-buying)

**Developer preview.** The Preview 2 simulator ran on a Raspberry Pi 5. Shelly plug and lamp control, optional LED and audio outputs, and automatic startup and trip recovery after a reboot still need physical testing. [Preview 2 release](https://github.com/jeffhudesman/vacation-ghost/releases/tag/v0.1.0-preview.2) · [Pi 5 test record](docs/hardware.md#raspberry-pi-5-simulator-test)

[![Watch the Vacation Ghost demo. Concept animation of house lights changing over an evening.](docs/images/vacation-ghost-demo.gif)](https://vacationghost.com/#sample-evening)

## Try it without hardware

On Linux, use Git, Python 3.11 or later, and the system time-zone database, usually provided by `tzdata`. The simulator uses Python’s standard library. The commands fetch Preview 2 and replay a fictional trip in a temporary directory.

```sh
(
  set -eu
  demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/vacation-ghost-demo.XXXXXX")
  git clone https://github.com/jeffhudesman/vacation-ghost.git \
    "$demo_dir/vacation-ghost"
  cd "$demo_dir/vacation-ghost"
  git checkout --quiet --detach 34d794e52c58b3cfb02027f728ae842d26ae03af
  python3 -m vacation_ghost \
    --config examples/vacation.example.toml simulate \
    --at 2030-06-11T09:00:00Z --state-dir "$demo_dir/state"
  cat "$demo_dir/state/daily/2030-06-11.json"
)
```

The JSON includes `"dry_run": true` and this morning report message.

> Simulation. Lamps ran between 7:34 PM and 10:45 PM local time (3 hr 11 min total).

The trip runs from June 10 to 13, 2030 in UTC. To replay the entire trip and generate `trip.json`, omit `--at`. Keep simulated reports in their own temporary state directory.

## Set up the Raspberry Pi

A lights-only setup uses a Raspberry Pi Zero 2 W, Pi 4, or Pi 5, two compatible Shelly Gen2 or Gen3 plugs, and two lamps that light up when power returns. You’ll need a local network whose plug access you can restrict. [Parts and compatibility](docs/hardware.md)

Install a verified release, enter your trip dates and plug addresses, then check both plugs with `doctor` and watch the lamps take turns with `rehearse`. Test power loss and recovery while you’re home before relying on the setup away. Add optional TV glow or quiet household sounds after the lamp checks pass. [Pi setup guide](docs/service.md#lights-only-quick-start) · [Lights-only example](examples/vacation.lights-only.toml)

<details>
<summary>Configuration reference</summary>

| Section | Settings |
| --- | --- |
| `[trip]` | Set `start` and `end` to ISO 8601 timestamps with UTC offsets. Use an IANA `timezone` such as `Etc/UTC` for the routine and reports. |
| `[routine]` | List the kitchen before the living room in `rooms`. Set local `sunset` from `15:00` to `22:00` and `bedtime` from `18:00` to `23:55`, with sunset earlier. When you set `latitude` and `longitude`, the routine calculates sunset and uses `sunset` as a fallback. An optional `seed` makes daily variation repeatable. |
| `[hardware]` | For lights only, use `led_count = 0` and `volume = 0`. Glow uses `led_count` and the BCM `led_pin`. Audio uses `sounds_dir` (relative to the config or absolute) and supports volume up to `0.20`. |
| `[hardware.plugs]` | Give each room a different private IPv4 address such as `http://192.168.1.40` or bracketed unique-local IPv6 address such as `http://[fd00::40]`. |

</details>

At trip end, Vacation Ghost attempts to turn the outputs off and reports whether shutdown was confirmed. Use `disarm` to end a trip early and `recover` to retry a pending shutdown. Keep the plug settings and state directory until shutdown is confirmed. [Stop or recover a trip](docs/service.md#stop-recover-or-change-trips) · [Plug safeguards](docs/hardware.md#restrict-plug-access)

## Read reports locally

Vacation Ghost uses no camera or microphone and collects no audio.

| File in the state directory | Purpose |
| --- | --- |
| `latest.json` | Current lamp, heartbeat, device faults, and shutdown status. |
| `daily/YYYY-MM-DD.json` | Previous evening’s activity, due at 8 AM in the trip’s time zone and named for its delivery date. |
| `trip.json` | Trip summary and shutdown status as of publication. Check `latest.json` after a later recovery. |

[Read reports on your Pi](docs/service.md#read-reports-locally)

<details>
<summary>JSON fields and local integrations</summary>

- `latest.json` is replaced atomically. Its fields include `active`, `lamp_room`, `updated_at`, `shutdown_unconfirmed`, `faults_by_device`, and `device_faults`.
- The controller checks both lamps at each 15-second heartbeat and corrects unexpected relay states. The lamps can continue running when audio or glow faults are reported.
- The daily report includes `delivery_date` and `night_of`. A `schedule_note` explains an evening shortened or skipped because sunset was close to bedtime.
- Daily and trip reports include `schema_version`, `trip_id`, and a stable `id`. Use `id` to send `message` once and skip reports with `dry_run` set to `true`. A malformed report is saved as a `.corrupt-*` copy and regenerated with the same ID.

Export only the needed reports and keep the controller’s state directory private. [Read-only export](docs/service.md#share-reports-locally)

</details>

## Contribute

The contributor guide covers the repository map, automated tests, package checks, and the local website preview. Vacation Ghost is open source under the MIT License. [Contributor guide](CONTRIBUTING.md) · [MIT License](LICENSE)
