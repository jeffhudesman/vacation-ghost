# Run Vacation Ghost at boot

The service attempts to turn off the outputs at trip end and reports whether shutdown was confirmed. When enabled at boot, it is designed to start automatically and resume an active trip after a reboot.

The Preview 2 simulator ran on a Raspberry Pi 5. Physical testing is pending for the Shelly plugs and lamps, the optional LED and audio outputs, and automatic startup and trip recovery after a reboot. Start with two switched lamps and use the [bench checklist](hardware.md#physical-release-checklist) before relying on the setup while away.

## Lights-only quick start

Use a Raspberry Pi Zero 2 W, Pi 4, or Pi 5 running Raspberry Pi OS. You’ll need a terminal with `sudo` access, two [compatible plugs](hardware.md#hardware-compatibility), and the ability to restrict access to the plugs on your local network. Use two lamps that turn on when power returns. If you already have an installation, see [how to upgrade or change profiles](#upgrade-or-switch-profiles) first.

1. [Prepare the Pi and plugs](#prepare-the-pi-and-plugs).
2. [Install from a verified release](#install-from-a-verified-release) and [set the trip and plug addresses](#set-the-trip-and-plug-addresses).
3. [Set up the lights-only service account](#use-the-lights-only-profile).
4. [Check both plugs, rehearse, and start the service](#check-and-start).
5. [Verify the setup while home](#verify-the-setup-while-home).

### Prepare the Pi and plugs

Flash a current Raspberry Pi OS image, boot the Pi, and sign in to a shell. Set up the [network access and plug safeguards](hardware.md#restrict-plug-access), including a reserved address, power-restore off, and a built-in auto-off timer for each plug.

On the Pi, install Git, Python’s virtual-environment tools, the system time-zone database, and HTTPS certificates from the signed Raspberry Pi OS repositories. Use Python 3.11 or later.

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates git python3-venv tzdata
/usr/bin/python3 --version
timedatectl status
```

The Pi needs a trustworthy clock after power loss. Use a battery-backed clock or configure your time synchronization provider and its boot wait service. The unit starts after `time-sync.target`, which waits for accurate network time when a synchronization service delays it.

If your Pi uses `systemd-timesyncd` for network time, turn on synchronization and enable its wait service:

```sh
sudo timedatectl set-ntp true
sudo systemctl enable systemd-time-wait-sync.service
systemctl is-enabled systemd-time-wait-sync.service
timedatectl show --property=NTPSynchronized --value
```

The checks should report `enabled` and `yes` once network time is available. For a different time provider, enable its equivalent boot wait service. Restart the Pi while home and before arming a trip, then check that the clock is accurate. A network wait service can delay Vacation Ghost until its time source becomes reachable. If you use a battery-backed clock, verify the time after disconnecting and restoring power.

Before continuing, confirm the reported Python version and the Pi’s clock. Test each plug’s power-restore and auto-off settings independently of the Pi.

### Install from a verified release

Open the [v0.1.0-preview.2 GitHub prerelease](https://github.com/jeffhudesman/vacation-ghost/releases/tag/v0.1.0-preview.2) and replace `REPLACE_WITH_FULL_SHA_FROM_RELEASE_NOTES` below with the full source commit ID shown there. If the tag is signed, verify it with the trusted maintainer key. The first block checks that the tag points at the published commit and stages that source for installation. Run both blocks in the same shell.

```sh
release_tag=v0.1.0-preview.2
release_commit=REPLACE_WITH_FULL_SHA_FROM_RELEASE_NOTES
source_dir=$(mktemp -d /tmp/vacation-ghost-source.XXXXXX)
release_dir=$(mktemp -d /tmp/vacation-ghost-release.XXXXXX)
(
  set -eu
  git clone --no-checkout https://github.com/jeffhudesman/vacation-ghost.git "$source_dir"
  cd "$source_dir"
  resolved_commit=$(git --no-replace-objects rev-parse --verify --end-of-options "refs/tags/$release_tag^{commit}")
  if [ "$resolved_commit" != "$release_commit" ]; then
    printf 'The tag does not match the full SHA in the release notes.\n' >&2
    exit 1
  fi
  # If the release notes identify a signed tag, verify it with the maintainer key.
  # git verify-tag "$release_tag"
  git --no-replace-objects archive --format=tar "$resolved_commit" > "$release_dir/source.tar"
  tar -xf "$release_dir/source.tar" -C "$release_dir"
  printf '%s\n' "$resolved_commit" > "$release_dir/.verified"
  printf 'Verified release commit: %s\n' "$resolved_commit"
)
```

Check that `Verified release commit` shows the same full commit ID as the release notes. The next block installs the app from the staged release and creates the private configuration if needed.

```sh
(
  set -eu
  [ "$(cat "$release_dir/.verified")" = "$release_commit" ]
  sudo install -d -o root -g root -m 0755 /opt/vacation-ghost
  sudo install -d -o root -g root -m 0755 /opt/vacation-ghost/vacation_ghost /opt/vacation-ghost/tools /opt/vacation-ghost/deploy
  sudo install -o root -g root -m 0644 "$release_dir"/vacation_ghost/*.py /opt/vacation-ghost/vacation_ghost/
  sudo install -o root -g root -m 0644 "$release_dir/tools/generate_sounds.py" /opt/vacation-ghost/tools/
  sudo install -o root -g root -m 0644 "$release_dir"/deploy/* /opt/vacation-ghost/deploy/
  sudo /usr/bin/python3 -I -m venv /opt/vacation-ghost/.venv

  if ! sudo test -d /etc/vacation-ghost; then
    sudo install -d -o root -g root -m 0700 /etc/vacation-ghost
  fi
  if ! sudo test -d /var/lib/vacation-ghost; then
    sudo install -d -o root -g root -m 0700 /var/lib/vacation-ghost
  fi
  if ! sudo test -e /etc/vacation-ghost/vacation.toml; then
    sudo install -o root -g root -m 0600 "$release_dir/examples/vacation.lights-only.toml" /etc/vacation-ghost/vacation.toml
  fi
)
```

### Set the trip and plug addresses

Replace the fictional dates in `/etc/vacation-ghost/vacation.toml`. Give each timestamp the UTC offset for that date in your named time zone. For example, a 9 AM summer departure in New York and a later return could use these settings.

```toml
[trip]
start = 2030-06-10T09:00:00-04:00
end = 2030-06-13T13:00:00-04:00
timezone = "America/New_York"
```

A timestamp ending in `Z` is UTC even if you change `timezone`. Set a future departure while commissioning the Pi. The fixed fallback sunset must be earlier than bedtime. When an optional local sunset calculation yields a short evening, the schedule may shorten or skip its evening activity.

In `[hardware.plugs]`, match the keys to the two names in `routine.rooms` and give each a different plug. Use a private IPv4 address such as `http://192.168.1.20` or bracket a unique-local IPv6 address in a URL such as `http://[fd00::20]`. Keep glow and sound disabled for the first install. The Pi contacts the plugs only over the restricted LAN. Keep real trip settings and plug addresses in the private Pi config or an ignored `.local/` directory during development. [Lights-only example](../examples/vacation.lights-only.toml)

Open the configuration and make those changes.

```sh
sudoedit /etc/vacation-ghost/vacation.toml
```

Before saving, confirm both addresses are distinct, the departure is in the future, and `led_count` and `volume` are both `0`.

### Use the lights-only profile

For two-plug installations with both optional outputs disabled, install the drop-in that runs the service as a dedicated account without capabilities or direct device access. Use the same account for manual hardware commands. The commands below assume a fresh deployment. [Upgrade and profile changes](#upgrade-or-switch-profiles)

```sh
(
  set -eu
  cd /opt/vacation-ghost
  if ! id vacation-ghost >/dev/null 2>&1; then
    sudo useradd --system --user-group --home-dir /var/lib/vacation-ghost \
      --shell /usr/sbin/nologin vacation-ghost
  fi
  sudo chown root:vacation-ghost /etc/vacation-ghost /etc/vacation-ghost/vacation.toml
  sudo chmod 0750 /etc/vacation-ghost
  sudo chmod 0640 /etc/vacation-ghost/vacation.toml
  sudo install -d -o vacation-ghost -g vacation-ghost -m 0700 /var/lib/vacation-ghost
  sudo -u vacation-ghost -g vacation-ghost test -r /etc/vacation-ghost/vacation.toml
  sudo -u vacation-ghost -g vacation-ghost test -w /var/lib/vacation-ghost
  sudo install -d -o root -g root -m 0755 /etc/systemd/system/vacation-ghost.service.d
  sudo install -o root -g root -m 0644 /opt/vacation-ghost/deploy/lights-only.conf \
    /etc/systemd/system/vacation-ghost.service.d/lights-only.conf
)
```

The permission checks in this block complete silently when the service account can read the config and write to the state directory.

### Check and start

Run the checks from the root-owned install. These commands use the lights-only `vacation-ghost` account. If you set up the base service for optional hardware, set `service_user=root` in the check, start, disarm, and recovery commands. `doctor` reads the relay state and device identity from each plug and checks that you configured two distinct devices. It checks for the optional driver and cue files when enabled. `rehearse` ignores trip dates and briefly drives the connected outputs. Run it when you are home, with the scheduled service stopped and using separate state.

```sh
(
  set -eu
  service_user=vacation-ghost
  cd /opt/vacation-ghost
  sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml doctor
  rehearsal_dir=$(sudo -u "$service_user" -g "$service_user" mktemp -d /var/lib/vacation-ghost/rehearsal.XXXXXX)
  printf 'Rehearsal state is in %s\n' "$rehearsal_dir"
  sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml rehearse --state-dir "$rehearsal_dir"
)
```

For lights only, `doctor` should show the current state of both plugs, with TV glow and sound listed as disabled. Watch the rehearsal to confirm the lamps take turns and both turn off at the end.

Install the unit from the verified release after rehearsal succeeds. Keep the departure far enough in the future to finish this check while the trip is idle. The commands require a stopped unit, start a new service invocation, and wait for a fresh idle status for the configured trip. Look for `Vacation Ghost is idle and waiting for departure` before the unit is enabled at boot.

```sh
(
  set -eu
  service_user=vacation-ghost
  unit=vacation-ghost.service
  cd /opt/vacation-ghost
  sudo install -o root -g root -m 0644 /opt/vacation-ghost/deploy/vacation-ghost.service /etc/systemd/system/vacation-ghost.service
  sudo systemd-analyze verify /etc/systemd/system/vacation-ghost.service
  sudo systemctl daemon-reload
  unit_user=$(sudo systemctl show --property=User --value "$unit")
  if [ "${unit_user:-root}" != "$service_user" ]; then
    printf 'Expected service account %s, found %s. Check the installed profile.\n' \
      "$service_user" "${unit_user:-root}" >&2
    exit 1
  fi

  unit_state=$(sudo systemctl show --property=ActiveState --value "$unit")
  case "$unit_state" in
    inactive|failed) ;;
    *)
      printf 'The service is %s. Follow the shutdown and retry instructions before commissioning again.\n' \
        "$unit_state" >&2
      exit 1
      ;;
  esac
  previous_restarts=$(sudo systemctl show --property=NRestarts --value "$unit")
  if [ "$unit_state" = failed ] || [ "$previous_restarts" != 0 ]; then
    sudo systemctl reset-failed "$unit"
  fi
  previous_invocation=$(sudo systemctl show --property=InvocationID --value "$unit")

  expected_trip=$(sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python \
    - /etc/vacation-ghost/vacation.toml <<'PY'
import sys
from datetime import UTC, datetime
from pathlib import Path

from vacation_ghost.config import load_config
from vacation_ghost.reports import trip_identity

config = load_config(Path(sys.argv[1]))
if config.start <= datetime.now(UTC):
    raise SystemExit("Set a future departure before starting the service.")
print(trip_identity(config.start, config.end))
PY
  )
  status_file=/var/lib/vacation-ghost/current/latest.json
  previous_inode=$(sudo -u "$service_user" -g "$service_user" stat -c '%i' "$status_file" 2>/dev/null || true)
  started_at=$(/usr/bin/python3 -c 'import time; print(time.time())')

  commissioning_failed() {
    printf 'Commissioning failed. Follow the shutdown and retry instructions below.\n' >&2
    sudo journalctl -u "$unit" -n 40 --no-pager || true
    exit 1
  }

  sudo systemctl start "$unit" || commissioning_failed
  invocation=$(sudo systemctl show --property=InvocationID --value "$unit") || commissioning_failed
  case "$invocation" in
    ""|00000000000000000000000000000000|"$previous_invocation") commissioning_failed ;;
  esac

  if ! idle_at=$(sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python \
    - "$status_file" "$previous_inode" "$started_at" "$expected_trip" <<'PY'
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

path, previous_inode, started_at, expected_trip = sys.argv[1:]
deadline = time.monotonic() + 35
while time.monotonic() < deadline:
    try:
        with Path(path).open() as stream:
            inode = str(os.fstat(stream.fileno()).st_ino)
            status = json.load(stream)
        if not isinstance(status, dict):
            raise ValueError("status must be a JSON object")
        updated_at = datetime.fromisoformat(status["updated_at"])
        fresh = (
            inode != previous_inode
            and updated_at.utcoffset() is not None
            and updated_at.timestamp() >= float(started_at)
        )
    except (OSError, ValueError, KeyError, TypeError):
        fresh = False
    if fresh:
        if (
            status.get("trip_id") == expected_trip
            and status.get("active") is False
            and status.get("disarmed") is False
            and status.get("completed") is False
            and status.get("shutdown_unconfirmed") is False
            and status.get("device_faults") == 0
        ):
            print(updated_at.isoformat())
            break
        raise SystemExit("Fresh status did not show the expected trip waiting without device faults.")
    time.sleep(1)
else:
    raise SystemExit("No fresh Vacation Ghost status was written within 35 seconds.")
PY
  ); then
    commissioning_failed
  fi
  current_invocation=$(sudo systemctl show --property=InvocationID --value "$unit") || commissioning_failed
  current_restarts=$(sudo systemctl show --property=NRestarts --value "$unit") || commissioning_failed
  if [ "$current_invocation" != "$invocation" ] ||
     [ "$current_restarts" != 0 ] ||
     ! sudo systemctl is-active --quiet "$unit"; then
    commissioning_failed
  fi
  printf 'Vacation Ghost is idle and waiting for departure (%s).\n' "$idle_at"
  sudo systemctl enable "$unit"
)
```

If the check fails, the unit may still be running. Inspect `sudo systemctl status vacation-ghost.service`, the journal, and the status with `sudo cat /var/lib/vacation-ghost/current/latest.json`. Keep the original config, trip state, and device reservation while you verify shutdown. If the trip is active or you cannot confirm it stayed idle, follow [Stop, recover, or change trips](#stop-recover-or-change-trips) to disarm it and handle any unconfirmed shutdown. If it stayed idle before departure, reports no pending shutdown, and the outputs are off, stop it with `sudo systemctl stop vacation-ghost.service`.

Retry only after the unit is stopped and the outputs are confirmed off. If you disarmed the trip, it completed, or you need to change its dates, follow [Start a new trip](#start-a-new-trip) to archive `current` first. The unit uses `/var/lib/vacation-ghost/device` for its device reservation. Preserve it across retries.

### Verify the setup while home

Complete the [physical release checklist](hardware.md#physical-release-checklist) with your Pi, Raspberry Pi OS image, and plug models and firmware. Run a short scheduled trip and check that the clock and routine recover after a reboot, the plug timers turn the lamps off when the Pi loses power or network access, and both lamps switch off when the trip ends. Compare what you observed with the [local reports](#read-reports-locally). The checklist includes separate tests for scheduled and early shutdown.

## Stop, recover, or change trips

Use `disarm` with the same config and trip state directory to end a trip early, including while the service runs. The disarm request persists across reboot. If the request queues, wait for the runner to finish and check `/var/lib/vacation-ghost/current/latest.json` to confirm shutdown. Keep the original plug settings and state directory until off is confirmed. Set `service_user=root` for the base service with optional hardware.

```sh
(
  set -eu
  service_user=vacation-ghost
  cd /opt/vacation-ghost
  sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml disarm --state-dir /var/lib/vacation-ghost/current
  sudo -u "$service_user" -g "$service_user" cat /var/lib/vacation-ghost/current/latest.json
)
```

If shutdown is unconfirmed, stop an installed scheduled service with `sudo systemctl stop vacation-ghost.service`. Check the lamps directly and fix any connection issue. Use `recover` with the original config and state directory. For a failed rehearsal, substitute its printed state directory for `current`. `recover` only attempts to turn outputs off. It may confirm shutdown and print a separate warning if it cannot refresh the existing reports.

```sh
(
  set -eu
  service_user=vacation-ghost
  cd /opt/vacation-ghost
  sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml recover --state-dir /var/lib/vacation-ghost/current
)
```

If recovery reports a damaged device reservation, leave the reservation, trip state, and original config together. Turn both plugs off using their local switches, disconnect power from the LED strip if fitted, and stop any remaining playback. Check the outputs directly and get help with the damaged record before starting another trip.

After confirmed shutdown, disable the unit to prevent automatic startup at boot.

### Development checkout diagnostics and recovery

The checks in this section apply to the current development checkout. For the tagged Preview 2 installation, use the shutdown and recovery steps above.

- `doctor` identifies unknown configuration keys before hardware checks. It shows the effective LED backend, GPIO pin, and pixel count when glow is enabled, and the configured volume when sound is enabled.
- Before operating outputs, a development checkout records each Shelly device’s identity. If a plug’s IP address was reassigned, restore the original plugs and their reserved addresses, then repeat `recover` with the original config and state. Recovery can turn off verified plugs and keeps the reservation while any required output remains unconfirmed.
- A Preview 2 reservation records a fingerprint of the original config. Prefer recovery with the original installation. The development checkout can attempt an off-only recovery using the matching config and warns when it has to rely on its addresses. Check both plugs directly.
- If recovery confirms shutdown but reports that it could not refresh the JSON status, restore the original state storage and rerun the same `recover` command. The development checkout preserves the shutdown confirmation so it can retry the report update.

### Start a new trip

Confirm shutdown with the original configuration and state directory. Disable startup at boot and stop the unit before archiving `current`. The archive contains the earlier trip’s disarm and completion records. If the old dates are still active, automatic startup with a fresh `current` could resume that trip. Keep `/var/lib/vacation-ghost/device` in place.

```sh
(
  set -eu
  sudo systemctl disable --now vacation-ghost.service
  if sudo test -d /var/lib/vacation-ghost/current; then
    trip_backup=$(sudo mktemp -d /var/lib/vacation-ghost/trip-previous.XXXXXX)
    sudo mv -T /var/lib/vacation-ghost/current "$trip_backup/current"
    printf 'Previous trip saved in %s\n' "$trip_backup/current"
  fi
)
```

Set the new trip dates and run [Check and start](#check-and-start) again. The service creates a fresh `current` directory and the commissioning check enables boot startup once the new trip is waiting.

## Upgrade or switch profiles

Before changing the installed files, unit, or output configuration, confirm that the old deployment has finished shutting down. Use its existing service account, original config, and original state directory. If the service is running, disarm it and wait for it to finish. Use `recover` for an unconfirmed shutdown. Then disable and stop the unit with `sudo systemctl disable --now vacation-ghost.service`.

For an upgrade with the same service profile, keep `/etc/vacation-ghost` and `/var/lib/vacation-ghost/device` in place. With the unit stopped and shutdown confirmed, follow [Start a new trip](#start-a-new-trip) to archive the previous trip’s `current` directory and set a new future departure and return. Save the old application directory so the new install starts with a fresh app and virtual environment. Reinstall the optional driver and regenerate or restore any trusted audio cues used by the configuration. Follow [Check and start](#check-and-start) to commission the new release.

```sh
(
  set -eu
  app_backup=$(sudo mktemp -d /opt/vacation-ghost-previous.XXXXXX)
  sudo mv -T /opt/vacation-ghost "$app_backup/app"
  printf 'Previous application saved in %s\n' "$app_backup/app"
)
```

For a profile change, move the old state into a private archive after shutdown is confirmed. The new account needs a fresh state tree because `/var/lib/vacation-ghost/device` belongs to the active profile. Use new trip dates when starting a new deployment.

```sh
(
  set -eu
  state_backup=$(sudo mktemp -d /var/lib/vacation-ghost-previous.XXXXXX)
  sudo mv -T /var/lib/vacation-ghost "$state_backup/state"
  printf 'Previous state saved in %s\n' "$state_backup/state"
)
```

To switch to lights-only, set `led_count = 0` and `volume = 0` in `/etc/vacation-ghost/vacation.toml`, then follow the [lights-only setup](#use-the-lights-only-profile). To switch from lights-only to the base service with device access, remove the lights-only drop-in, restore the root-only config and state permissions, and enable the optional hardware you intend to use.

```sh
(
  set -eu
  sudo rm -f /etc/systemd/system/vacation-ghost.service.d/lights-only.conf
  sudo chown root:root /etc/vacation-ghost /etc/vacation-ghost/vacation.toml
  sudo chmod 0700 /etc/vacation-ghost
  sudo chmod 0600 /etc/vacation-ghost/vacation.toml
  sudo install -d -o root -g root -m 0700 /var/lib/vacation-ghost
  sudo systemctl daemon-reload
)
```

## Optional TV glow and sound

For a first build, start with the [lights-only hardware checks](#verify-the-setup-while-home). To add glow or sound to a lights-only installation, follow the [profile change procedure](#upgrade-or-switch-profiles) to switch to the base service. For a fresh base installation, [prepare the Pi](#prepare-the-pi-and-plugs), [install the release](#install-from-a-verified-release), and [set the trip and plug addresses](#set-the-trip-and-plug-addresses) before configuring the optional outputs below. The base service uses the root account.

Preview 2’s TV-glow setup uses the `rpi-ws281x` PWM driver on a Pi Zero 2 W or Pi 4. See the [LED driver and board requirements](hardware.md#optional-glow-and-sound) and the separate [Pi 5 bench instructions](#try-pi-5-tv-glow-from-a-development-checkout). In `[hardware]`, set `led_count` to the number of connected pixels and use `led_pin = 18` for BCM GPIO18 on physical header pin 12. This recipe uses PWM channel 0 and an 800 kHz GRB strip. The base service runs as root for PWM/DMA access. If your OS offers a compatible distribution-signed driver package, install it through the OS package manager and run `sudo /usr/bin/python3 -I -m venv --system-site-packages /opt/vacation-ghost/.venv` so the service can load it. [Wiring guide](hardware.md#wire-the-optional-led-strip)

At the pinned release, [PyPI lists `rpi-ws281x` 5.0.0 as a source distribution](https://pypi.org/project/rpi-ws281x/5.0.0/#files). Build its wheel as an ordinary user on the target Pi and OS. The [checked-in requirements file](../deploy/rpi-ws281x-source.txt) pins the upstream source URL and digest. These instructions use compiler and Python build tools from signed OS repositories.

```sh
sudo apt-get install -y build-essential python3-dev python3-setuptools python3-wheel
build_dir=$(mktemp -d /tmp/vacation-ghost-driver.XXXXXX)
python3 -m venv --system-site-packages "$build_dir/venv"
PYTHONNOUSERSITE=1 PYTHONPATH= CC=gcc CXX=g++ "$build_dir/venv/bin/python" -I -m pip --isolated wheel --no-index --no-deps \
  --no-build-isolation --require-hashes --wheel-dir "$build_dir/wheels" \
  -r /opt/vacation-ghost/deploy/rpi-ws281x-source.txt
```

Once the build succeeds, stage the locally built wheel and install it offline. The wheel digest depends on trust in the local build account and tools. If using a compatible prebuilt upstream wheel from a reviewed release, check its Python ABI and architecture tags, place it in a fresh `$build_dir/wheels` directory, and set `upstream_wheel_digest` to the SHA-256 of that exact file from the release's PyPI metadata. An installer check confirms the file and Python ABI. A [physical rehearsal](hardware.md#physical-release-checklist) confirms GPIO, PWM/DMA, and wiring compatibility.

```sh
(
  set -eu
  wheel=$(printf '%s\n' "$build_dir"/wheels/rpi_ws281x-*.whl)
  wheel_name=${wheel##*/}
  wheel_digest=$(sha256sum "$wheel")
  wheel_digest=${wheel_digest%% *}
  [ "$wheel_digest" = "${upstream_wheel_digest:-$wheel_digest}" ]
  printf 'rpi-ws281x @ file:///opt/vacation-ghost/wheels/%s --hash=sha256:%s\n' \
    "$wheel_name" "$wheel_digest" > "$build_dir/wheel-lock.txt"
  sudo install -d -o root -g root -m 0755 /opt/vacation-ghost/wheels
  sudo install -o root -g root -m 0644 "$wheel" "$build_dir/wheel-lock.txt" /opt/vacation-ghost/wheels/
  sudo /opt/vacation-ghost/.venv/bin/python -I -m pip --isolated install --no-index --no-deps \
    --only-binary=:all: --require-hashes --force-reinstall -r /opt/vacation-ghost/wheels/wheel-lock.txt
)
```

For sound, add `sounds_dir = "/opt/vacation-ghost/sounds"` under `[hardware]` and set `volume` to a low nonzero value, such as `0.05`. Install ALSA tools and use `aplay -l` to identify the USB audio adapter. Set its ALSA default in a system-visible configuration such as `/etc/asound.conf`. The unit’s `ProtectHome=true` hides per-user files such as `/root/.asoundrc`. Generate the cues at the configured path using the staged program.

```sh
sudo apt-get install -y alsa-utils
sudo /opt/vacation-ghost/.venv/bin/python -I /opt/vacation-ghost/tools/generate_sounds.py /opt/vacation-ghost/sounds
```

Follow [Check and start](#check-and-start) with `service_user=root`, then run a short scheduled test trip under the systemd unit. Choose a window with the glow or sound cues you enabled. Observe the outputs and check `latest.json` and the unit journal for faults. The foreground rehearsal uses the same account, and the systemd test exercises the unit’s filesystem and device settings. Disarm and verify shutdown before [preparing the real trip](#stop-recover-or-change-trips). [Physical checklist](hardware.md#physical-release-checklist)

### Try Pi 5 TV glow from a development checkout

Use a Raspberry Pi 5 with Raspberry Pi OS, two configured Shelly plugs, and an 800 kHz WS2812B-compatible GRB strip. This bench procedure uses the SPI driver from `main`. Preview 2 uses the PWM recipe above. Physical glow and blackout tests are pending.

Start with a Pi that has no running Vacation Ghost installation. If you already have one, confirm shutdown using its original config and state, then disable and stop the unit. This test runs as root and shares the device reservation at `/var/lib/vacation-ghost/device`. Follow the [profile change procedure](#upgrade-or-switch-profiles) if a different service account owns the existing state. Keep the bench test separate from the scheduled unit.

With both power supplies disconnected, [wire the strip](hardware.md#wire-the-optional-led-strip) using BCM GPIO10 on header pin 19. Reserve all of SPI0 for the strip. Check the wiring, then reconnect both supplies. Check that your Raspberry Pi OS repository has a candidate for `python3-spidev`.

```sh
sudo apt-get update
apt-cache policy python3-spidev
```

Install the packages from the OS repository and enable SPI. Confirm both lamps and the strip are off before rebooting the Pi.

```sh
(
  set -eu
  sudo apt-get install -y git python3-spidev tzdata
  sudo raspi-config nonint do_spi 0
  sudo reboot
)
```

After reboot, check that Linux exposes SPI0 and that its system Python can import the adapter. Clone `main`, record the commit, and review the code before staging the files that will run as root.

```sh
test -c /dev/spidev0.0
/usr/bin/python3 -I -c 'import spidev; print(spidev.__file__)'
spi_checkout=$(mktemp -d /tmp/vacation-ghost-spi-source.XXXXXX)
git clone --depth 1 --branch main https://github.com/jeffhudesman/vacation-ghost.git "$spi_checkout"
git -C "$spi_checkout" log -1 --oneline
```

In the same shell, stage the reviewed code and a private config in a separate root-owned directory.

```sh
spi_app=$(
  set -eu
  target=$(sudo mktemp -d /opt/vacation-ghost-spi.XXXXXX)
  sudo install -d -o root -g root -m 0700 "$target/vacation_ghost"
  sudo install -o root -g root -m 0644 "${spi_checkout:?}"/vacation_ghost/*.py "$target/vacation_ghost/"
  sudo install -o root -g root -m 0600 \
    "$spi_checkout/examples/vacation.lights-only.toml" "$target/vacation.toml"
  printf '%s\n' "$target"
)
```

Run `sudoedit "${spi_app:?}/vacation.toml"`. Enter your two plug addresses and trip settings. In the existing `[hardware]` section, set the SPI backend and use the actual count of connected pixels, up to 144. Leave `led_pin` unset to use BCM GPIO10. Keep `volume = 0` for the first glow rehearsal.

```toml
[hardware]
led_backend = "spi"
led_count = 30
volume = 0
```

Run `doctor` to check SPI0 adapter access and reachability for both plugs without sending an LED frame. Then run a foreground rehearsal while you are home. Save the two printed paths.

```sh
rehearsal_dir=$(sudo mktemp -d "${spi_app:?}/rehearsal.XXXXXX")
printf 'Bench config is in %s/vacation.toml\nRehearsal state is in %s\n' "$spi_app" "$rehearsal_dir"
sudo sh -c '
  set -eu
  cd "$1"
  /usr/bin/python3 -Es -m vacation_ghost --config "$1/vacation.toml" doctor
  exec /usr/bin/python3 -Es -m vacation_ghost --config "$1/vacation.toml" rehearse --state-dir "$2"
' sh "${spi_app:?}" "${rehearsal_dir:?}"
```

Watch both lamps and the strip. Check TV glow, color, and the final blackout. Record the Pi OS and kernel versions, development commit, strip model, and observations in the [physical checklist](hardware.md#physical-release-checklist). Keep the scheduled unit disabled during the bench test.

If shutdown is unconfirmed, leave the original config, wiring, and state in place. Inspect the outputs and fix the connection issue. Retry `recover` with the same bench paths.

```sh
sudo sh -c '
  cd "$1" || exit
  exec /usr/bin/python3 -Es -m vacation_ghost --config "$1/vacation.toml" recover --state-dir "$2"
' sh "${spi_app:?}" "${rehearsal_dir:?}"
```

If needed, set `spi_app` and `rehearsal_dir` from the saved paths before running recovery in a new shell. Confirm both lamps and the strip are off before changing the configuration, removing bench files, or restarting the scheduled unit. If shutdown remains unconfirmed, follow the [manual shutdown procedure](#stop-recover-or-change-trips).

## Read reports locally

The previous evening’s note is due at 8 AM in the trip’s time zone on mornings during the trip. If the service is stopped then, it can publish a due note when it runs again. The report is a local JSON file at `/var/lib/vacation-ghost/current/daily/YYYY-MM-DD.json`, where `YYYY-MM-DD` is the delivery date. Replace this sample date with the note you want to read. Use `root` for the user and group if running the base service. Read the `message` field for the summary.

```sh
delivery_date=2030-06-11
sudo -u vacation-ghost -g vacation-ghost cat \
  "/var/lib/vacation-ghost/current/daily/$delivery_date.json"
```

`latest.json` shows the current state and whether any pending shutdown has been confirmed. `trip.json` preserves what was known when the final report was published, including a shutdown fault that a later recovery may have resolved.

When the development checkout writes `latest.json` or `trip.json`, it includes `relay_off_verified` and `led_black_frame_sent` as booleans or `null`. A true relay value means the controller read back the off state. A true LED value means the driver sent a black frame. Check the lamps and strip directly during a physical test.

## Share reports locally

Keep `/var/lib/vacation-ghost` private. If a local notification service needs a report, export only the required fields with a trusted local copier. Use a separate directory owned by the writer and readable by a dedicated reader group. Set its mode to `2750` so files created there inherit the reader group, or use mode `0750` and explicitly assign the reader group to each staged file. Stage each export in that directory, set its group and mode `0640` before atomically renaming it into place, and verify the reader can open the export without write access. Keep trip settings, activity journals, device locks, and disarm records in the controller's private directories.
