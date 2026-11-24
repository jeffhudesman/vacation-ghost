# Run Vacation Ghost at boot

The service runs on the Pi, resumes the configured trip after a reboot, and exits when the trip ends. Set up the [hardware](hardware.md) and test it on site. The Pi needs a trustworthy clock after power loss. Use a battery-backed clock or a time synchronization provider with its corresponding boot wait service. `time-sync.target` provides ordering. Verify the clock actually synchronizes after power returns before depending on the service.

## Install from a verified release

Run on Raspberry Pi OS. Obtain the full release commit ID from a trusted maintainer or deployment channel and compare it with the commit in your checkout. If the release includes a signed tag, verify it with the trusted maintainer key. Extract from the verified Git object so working-tree files and local edits stay out of the root-owned installation. For an upgrade, follow the [stop procedure](#stop-recover-or-change-trips) first.

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
    sudo install -o root -g root -m 0600 "$release_dir/examples/vacation.example.toml" /etc/vacation-ghost/vacation.toml
  fi
  sudoedit /etc/vacation-ghost/vacation.toml
)
```

Set your local trip dates, time zone, rooms, and plug addresses. The lights-only configuration uses `led_count = 0` and `volume = 0` in `[hardware]`, plus a `[hardware.plugs]` table for both rooms. Leave `sounds_dir` out for lights-only use. Keep the plug RPC on its restricted LAN segment. The app does not listen on a network port or require Internet access at runtime.

## Optional TV glow and sound

With glow enabled, the unit runs as root for PWM/DMA access on the Pi models that require it. If your OS offers a compatible distribution-signed driver package, install it through the OS package manager and run `sudo /usr/bin/python3 -I -m venv --system-site-packages /opt/vacation-ghost/.venv` so the service can load it.

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

For sound, set `sounds_dir = "/opt/vacation-ghost/sounds"` and start with a low `volume`. Install ALSA tools and make USB audio the default for the service account. Generate the cues using the staged program.

```sh
sudo apt-get install -y alsa-utils
sudo /opt/vacation-ghost/.venv/bin/python -I /opt/vacation-ghost/tools/generate_sounds.py /opt/vacation-ghost/sounds
```

## Use the lights-only profile

For two-plug installations with both optional outputs disabled, install the drop-in that runs the service as a dedicated account without capabilities or direct device access. Use the same account for manual hardware commands. The commands below assume a fresh deployment. To switch profiles, first confirm shutdown using the existing service account, stop the service, and archive the old state before starting with a fresh deployment directory.

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

Run the checks from the root-owned install. The examples use `sudo` for the PWM/DMA profile. For the lights-only profile, use `sudo -u vacation-ghost` for the Python and `mktemp` commands, and keep `sudo` for systemd administration. `doctor` checks each plug through read-only `Switch.GetStatus`. `rehearse` ignores trip dates and briefly drives the connected outputs. Run it when you are home, with the scheduled service stopped and using separate state.

```sh
cd /opt/vacation-ghost
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml doctor
rehearsal_dir=$(sudo mktemp -d /var/lib/vacation-ghost/rehearsal.XXXXXX)
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml rehearse --state-dir "$rehearsal_dir"
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

Use `disarm` with the same config and trip state directory to end a trip early, including while the service runs. The disarm request persists across reboot. If the request queues, wait for the runner to finish and check `/var/lib/vacation-ghost/current/latest.json` to confirm shutdown. Keep the original plug settings and state directory until off is confirmed.

```sh
cd /opt/vacation-ghost
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml disarm \
  --state-dir /var/lib/vacation-ghost/current
sudo cat /var/lib/vacation-ghost/current/latest.json
```

If shutdown is unconfirmed, stop the service, check the lamps directly, and fix any connection issue. Use `recover` with the original config and state directory, including the original rehearsal directory if that is where shutdown failed. `recover` only attempts to turn outputs off and never runs the routine. For lights-only, run `disarm` and `recover` as `vacation-ghost`.

```sh
sudo systemctl stop vacation-ghost.service
cd /opt/vacation-ghost
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml recover \
  --state-dir /var/lib/vacation-ghost/current
```

After confirmed shutdown, disable the unit if you want to prevent startup at boot. To prepare a new trip, stop the unit, archive `current` under a fresh name, edit the dates, and run the setup checks before starting again. The service creates the next trip's `current` directory.

## Share reports locally

Keep `/var/lib/vacation-ghost` private. If a local notification service needs a report, export only the required report or fields using a trusted local copier. Use a separate export directory owned by the controller or root, readable by a dedicated reader group. Readers only need read and directory traversal permission. Set the export directory to `0750`, published files to `0640`, and have the writer publish by atomic replacement. Keep private configuration, activity journals, device locks, and disarm requests out of the export. Leave both application accounts separate and give the reader no write access to the controller's directories or exported files.
