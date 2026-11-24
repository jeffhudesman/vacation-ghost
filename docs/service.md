# Run Vacation Ghost at boot

The service runs on the Pi, resumes the configured trip after a reboot, and exits when the trip ends. Set up the [hardware](hardware.md) and test it on site. The Pi needs a trustworthy clock after power loss. Use a battery-backed clock or a time synchronization provider with its corresponding boot wait service. `time-sync.target` provides ordering. Verify the clock actually synchronizes after power returns before depending on the service.

## Install from a verified release

Run on Raspberry Pi OS. Obtain the full release commit ID from a trusted maintainer or deployment channel and compare it with the commit in your checkout. If the release includes a signed tag, verify it with the trusted maintainer key. Extract from the verified Git object so working-tree files and local edits stay out of the root-owned installation. Prepare an existing installation before upgrading or switching profiles. [Upgrade and profile changes](#upgrade-or-switch-profiles)

```sh
release_commit=REPLACE_WITH_TRUSTED_FULL_COMMIT_ID
release_dir=$(mktemp -d /tmp/vacation-ghost-release.XXXXXX)
(
  set -eu
  resolved_commit=$(git --no-replace-objects rev-parse --verify --end-of-options "$release_commit^{commit}")
  [ "$resolved_commit" = "$release_commit" ]
  git --no-replace-objects archive --format=tar "$resolved_commit" > "$release_dir/source.tar"
  tar -xf "$release_dir/source.tar" -C "$release_dir"
  touch "$release_dir/.verified"
)
```

Continue using that staged directory. These commands install only the files used to run the app. Use the signed Raspberry Pi OS package repositories for system tools.

```sh
(
  set -eu
  [ -f "$release_dir/.verified" ]
  sudo apt-get update
  sudo apt-get install -y python3-venv
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

Replace the fictional trip dates and plug addresses in `/etc/vacation-ghost/vacation.toml`. Set your local time zone and match the two plug keys to the room names. The example starts with glow and sound disabled. Keep the plug RPC on its restricted LAN segment. The app does not listen on a network port or require Internet access at runtime. [Lights-only example](../examples/vacation.lights-only.toml)

## Optional TV glow and sound

For glow, edit the existing `[hardware]` section. Set `led_count` to the number of connected pixels and set `led_pin` to the BCM GPIO used for data, such as `18`. The base service runs as root for PWM/DMA access on Pi models that require it. If your OS offers a compatible distribution-signed driver package, install it through the OS package manager and run `sudo /usr/bin/python3 -I -m venv --system-site-packages /opt/vacation-ghost/.venv` so the service can load it.

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

For sound, add `sounds_dir = "/opt/vacation-ghost/sounds"` under `[hardware]` and set `volume` to a low nonzero value, such as `0.05`. Install ALSA tools and make USB audio the default for the service account. Generate the cues at that same path using the staged program.

```sh
sudo apt-get install -y alsa-utils
sudo /opt/vacation-ghost/.venv/bin/python -I /opt/vacation-ghost/tools/generate_sounds.py /opt/vacation-ghost/sounds
```

## Use the lights-only profile

For two-plug installations with both optional outputs disabled, install the drop-in that runs the service as a dedicated account without capabilities or direct device access. Use the same account for manual hardware commands. The commands below assume a fresh deployment. [Upgrade and profile changes](#upgrade-or-switch-profiles)

```sh
if ! id vacation-ghost >/dev/null 2>&1; then
  sudo useradd --system --user-group --home-dir /var/lib/vacation-ghost \
    --shell /usr/sbin/nologin vacation-ghost
fi
sudo chown root:vacation-ghost /etc/vacation-ghost /etc/vacation-ghost/vacation.toml
sudo chmod 0750 /etc/vacation-ghost
sudo chmod 0640 /etc/vacation-ghost/vacation.toml
sudo install -d -o vacation-ghost -g vacation-ghost -m 0700 /var/lib/vacation-ghost
sudo install -d -o root -g root -m 0755 /etc/systemd/system/vacation-ghost.service.d
sudo install -o root -g root -m 0644 /opt/vacation-ghost/deploy/lights-only.conf \
  /etc/systemd/system/vacation-ghost.service.d/lights-only.conf
```

## Check and start

Run the checks from the root-owned install. Set `service_user=vacation-ghost` in the command if you installed the lights-only profile. `doctor` checks each plug through read-only `Switch.GetStatus`. `rehearse` ignores trip dates and briefly drives the connected outputs. Run it when you are home, with the scheduled service stopped and using separate state.

```sh
(
  set -eu
  service_user=root
  cd /opt/vacation-ghost
  sudo -u "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml doctor
  rehearsal_dir=$(sudo -u "$service_user" mktemp -d /var/lib/vacation-ghost/rehearsal.XXXXXX)
  printf 'Rehearsal state is in %s\n' "$rehearsal_dir"
  sudo -u "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml rehearse --state-dir "$rehearsal_dir"
)
```

Install the unit from the verified release. Enable it only after rehearsal succeeds.

```sh
sudo install -o root -g root -m 0644 /opt/vacation-ghost/deploy/vacation-ghost.service /etc/systemd/system/vacation-ghost.service
sudo systemd-analyze verify /etc/systemd/system/vacation-ghost.service
sudo systemctl daemon-reload
sudo systemctl enable --now vacation-ghost.service
sudo systemctl status vacation-ghost.service
sudo journalctl -u vacation-ghost.service -n 40
```

The unit uses `/var/lib/vacation-ghost/current` for the active trip and `/var/lib/vacation-ghost/device` for its device reservation. Preserve both across restarts. The service can wait for a future trip start and exits after a successful trip. Check `current/latest.json` for `updated_at`, `shutdown_unconfirmed`, and device faults. Configure the plugs' own auto-off timers and power-restore behavior as described in the [hardware guide](hardware.md#restrict-plug-access).

## Stop, recover, or change trips

Use `disarm` with the same config and trip state directory to end a trip early, including while the service runs. The disarm request persists across reboot. If the request queues, wait for the runner to finish and check `/var/lib/vacation-ghost/current/latest.json` to confirm shutdown. Keep the original plug settings and state directory until off is confirmed. Set `service_user=vacation-ghost` in these commands if using the lights-only profile.

```sh
(
  set -eu
  service_user=root
  cd /opt/vacation-ghost
  sudo -u "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml disarm --state-dir /var/lib/vacation-ghost/current
  sudo cat /var/lib/vacation-ghost/current/latest.json
)
```

If shutdown is unconfirmed, stop an installed scheduled service with `sudo systemctl stop vacation-ghost.service`. Check the lamps directly and fix any connection issue. Use `recover` with the original config and state directory. For a failed rehearsal, substitute its printed state directory for `current`. `recover` only attempts to turn outputs off. It may confirm shutdown and print a separate warning if it cannot refresh the existing reports.

```sh
(
  set -eu
  service_user=root
  cd /opt/vacation-ghost
  sudo -u "$service_user" /opt/vacation-ghost/.venv/bin/python -m vacation_ghost \
    --config /etc/vacation-ghost/vacation.toml recover --state-dir /var/lib/vacation-ghost/current
)
```

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

## Share reports locally

Keep `/var/lib/vacation-ghost` private. If a local notification service needs a report, export only the required report or fields using a trusted local copier. Use a separate export directory owned by the controller or root, readable by a dedicated reader group. Readers only need read and directory traversal permission. Set the export directory to `0750`, published files to `0640`, and have the writer publish by atomic replacement. Keep private configuration, activity journals, device locks, and disarm requests out of the export. Leave both application accounts separate and give the reader no write access to the controller's directories or exported files.
