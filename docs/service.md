# Run Vacation Ghost at boot

The service runs on the Pi, resumes the configured trip after a reboot, and attempts and reports shutdown at the trip’s end. Physical validation is pending. Start with two switched lamps and use the [bench checklist](hardware.md#physical-release-checklist) before relying on the setup while away.

## Lights-only quick start

1. Flash a current Raspberry Pi OS image, boot the Pi, and sign in to a shell with sudo access. Set up the [two plugs and their safeguards](hardware.md#restrict-plug-access).
2. [Install the verified preview source](#install-from-a-verified-release). Edit the private config with the two plug addresses and a future start time.
3. [Set up the lights-only service account](#use-the-lights-only-profile).
4. [Run the read-only checks and rehearse](#check-and-start). Start the unit and confirm a fresh status before enabling it at boot.

## Install from a verified release

On the Pi, install Git, Python’s virtual-environment tools, the system time-zone database, and HTTPS certificates from the signed Raspberry Pi OS repositories. Use Python 3.11 or later.

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates git python3-venv tzdata
/usr/bin/python3 --version
timedatectl status
```

The Pi needs a trustworthy clock after power loss. Use a battery-backed clock or configure your time synchronization provider and its boot wait service. The unit orders startup after `time-sync.target`. Verify that the clock synchronizes after power returns.

Open the published [v0.1.0-preview.1 GitHub prerelease](https://github.com/jeffhudesman/vacation-ghost/releases/tag/v0.1.0-preview.1) and copy the full source commit SHA from its release notes. If the tag is signed, verify it with the trusted maintainer key. The clone step checks that the named tag points at the published commit and archives that Git object for installation. Run the installer blocks in the same shell. Prepare an existing installation before upgrading or changing profiles. [Upgrade and profile changes](#upgrade-or-switch-profiles)

```sh
release_tag=v0.1.0-preview.1
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
)
```

The next block installs the app from the staged release and opens its private configuration in your editor.

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
  sudoedit /etc/vacation-ghost/vacation.toml
)
```

Replace the fictional dates in `/etc/vacation-ghost/vacation.toml`. Give each timestamp the UTC offset for that date in your named time zone. For example, a 9 AM summer departure in New York and a later return could use these settings.

```toml
[trip]
start = 2030-06-10T09:00:00-04:00
end = 2030-06-13T13:00:00-04:00
timezone = "America/New_York"
```

A timestamp ending in `Z` is UTC even if you change `timezone`. Set a future departure while commissioning the Pi. The fixed fallback sunset must be earlier than bedtime. When an optional local sunset calculation yields a short evening, the schedule may shorten or skip its evening activity.

In `[hardware.plugs]`, match the keys to the two names in `routine.rooms` and give each a different plug. Use a private IPv4 address such as `http://192.168.1.20` or bracket a unique-local IPv6 address in a URL such as `http://[fd00::20]`. Keep glow and sound disabled for the first install. The Pi contacts the plugs only over the restricted LAN. Keep real trip settings and plug addresses in the private Pi config or an ignored `.local/` directory during development. [Lights-only example](../examples/vacation.lights-only.toml)

## Optional TV glow and sound

For glow, edit the existing `[hardware]` section. Set `led_count` to the number of connected pixels and use `led_pin = 18` for BCM GPIO18 on physical header pin 12. The supported profile uses PWM channel 0 and an 800 kHz GRB strip. The base service runs as root for PWM/DMA access. If your OS offers a compatible distribution-signed driver package, install it through the OS package manager and run `sudo /usr/bin/python3 -I -m venv --system-site-packages /opt/vacation-ghost/.venv` so the service can load it. [Wiring guide](hardware.md#wire-the-optional-led-strip)

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

## Use the lights-only profile

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

## Check and start

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

Install the unit from the verified release after rehearsal succeeds. Keep the configured trip start in the future for this first check. The commands verify the unit, start it, and wait for a fresh idle status with no device faults or unconfirmed shutdown. Successful process startup alone does not mean the app loaded its configuration.

```sh
(
  set -eu
  service_user=vacation-ghost
  cd /opt/vacation-ghost
  sudo install -o root -g root -m 0644 /opt/vacation-ghost/deploy/vacation-ghost.service /etc/systemd/system/vacation-ghost.service
  sudo systemd-analyze verify /etc/systemd/system/vacation-ghost.service
  sudo systemctl daemon-reload
  unit_user=$(sudo systemctl show --property=User --value vacation-ghost.service)
  if [ "${unit_user:-root}" != "$service_user" ]; then
    printf 'Expected service account %s, found %s. Check the installed profile.\n' \
      "$service_user" "${unit_user:-root}" >&2
    exit 1
  fi
  status_file=/var/lib/vacation-ghost/current/latest.json
  previous_inode=$(sudo -u "$service_user" -g "$service_user" stat -c '%i' "$status_file" 2>/dev/null || true)
  started_at=$(/usr/bin/python3 -c 'import time; print(time.time())')
  sudo systemctl start vacation-ghost.service
  if ! sudo -u "$service_user" -g "$service_user" /opt/vacation-ghost/.venv/bin/python \
    - "$status_file" "$previous_inode" "$started_at" <<'PY'
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

path, previous_inode, started_at = sys.argv[1:]
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
            status.get("active") is False
            and status.get("disarmed") is False
            and status.get("completed") is False
            and status.get("shutdown_unconfirmed") is False
            and status.get("device_faults") == 0
        ):
            print(f"Vacation Ghost is idle and waiting for departure ({updated_at.isoformat()}).")
            break
        raise SystemExit("Fresh status did not show a healthy scheduled trip waiting for departure.")
    time.sleep(1)
else:
    raise SystemExit("No fresh Vacation Ghost status was written within 35 seconds.")
PY
  then
    sudo journalctl -u vacation-ghost.service -n 40 --no-pager
    exit 1
  fi
  sudo systemctl is-active --quiet vacation-ghost.service
  sudo systemctl enable vacation-ghost.service
)
```

If the check fails, read `sudo journalctl -u vacation-ghost.service -n 40 --no-pager` and check the current status before trying again. The unit uses `/var/lib/vacation-ghost/current` for trip state and `/var/lib/vacation-ghost/device` for its device reservation. Preserve both across restarts. A scheduled end sets `completed`, and an early stop requested with `disarm` sets `disarmed`. Both prevent that trip from starting again after reboot. A true `shutdown_unconfirmed` means the controller has yet to confirm that all required outputs are off. Inspect the lamps and use the recovery procedure when needed. [Stop or recover](#stop-recover-or-change-trips)

If you enabled glow or sound, also run a short scheduled test trip under the actual systemd unit. Choose a window that includes the evening glow and at least one sound cue. Observe the outputs and check `latest.json` and the unit journal for faults. The foreground rehearsal uses the same account, and the systemd test exercises the unit’s filesystem and device settings. Disarm and verify shutdown before [preparing the real trip](#stop-recover-or-change-trips). [Physical checklist](hardware.md#physical-release-checklist)

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

After confirmed shutdown, disable the unit if you want to prevent startup at boot. To prepare a new trip, stop the unit, archive `current` under a fresh name, edit the dates, and run the setup checks before starting again. The service creates the next trip's `current` directory.

## Upgrade or switch profiles

Before changing the installed files, unit, or output configuration, confirm that the old deployment has finished shutting down. Use its existing service account, original config, and original state directory. If the service is running, disarm it and wait for it to finish. Use `recover` for an unconfirmed shutdown. Then disable and stop the unit with `sudo systemctl disable --now vacation-ghost.service`.

For an upgrade, keep `/etc/vacation-ghost` and `/var/lib/vacation-ghost` in place. Save the old application directory so the new install starts with a fresh app and virtual environment. Reinstall the optional driver and regenerate or restore any trusted audio cues used by the configuration. Check the new release with `doctor` and `rehearse` before enabling the unit again.

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

## Read reports locally

The morning note is due at 8 AM in the trip’s time zone on mornings during the trip. It is saved as `/var/lib/vacation-ghost/current/daily/YYYY-MM-DD.json`, where `YYYY-MM-DD` is the delivery date. Replace this sample date with the note you want to read. Use `root` for the user and group if running the base service.

```sh
delivery_date=2030-06-11
sudo -u vacation-ghost -g vacation-ghost cat \
  "/var/lib/vacation-ghost/current/daily/$delivery_date.json"
```

`latest.json` shows the current state and whether any pending shutdown has been confirmed. `trip.json` preserves what was known when the final report was published, including a shutdown fault that a later recovery may have resolved.

## Share reports locally

Keep `/var/lib/vacation-ghost` private. If a local notification service needs a report, export only the required fields with a trusted local copier. Use a separate directory owned by the writer and readable by a dedicated reader group. Set its mode to `2750` so files created there inherit the reader group, or use mode `0750` and explicitly assign the reader group to each staged file. Stage each export in that directory, set its group and mode `0640` before atomically renaming it into place, and verify the reader can open the export without write access. Keep trip settings, activity journals, device locks, and disarm records in the controller's private directories.
