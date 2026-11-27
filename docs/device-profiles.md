# Install the development build on a Raspberry Pi

Use the current development code on Pi Zero 2 W, Pi 4, or Pi 5 with two compatible Shelly plugs. Each setup runs as the dedicated `vacation-ghost` account. The tagged Preview 2 simulator ran on a physical Pi 5. Relay operation, optional playback and glow, boot, and recovery under these profiles still need physical verification. [Hardware requirements](hardware.md#hardware-compatibility)

## Lights-only installation

1. Prepare the Pi and plugs, then [stage the development build](#stage-the-development-build).
2. Enter a future trip and both plug addresses. [Create the service account and install `lights-only.conf`](service.md#use-the-lights-only-profile). Keep `led_count = 0` and `volume = 0`. Those two lamps need no SPI or audio packages, device drop-ins, or supplementary groups.
3. With `lights-only.conf` in place, [pair, rehearse, and commission](#pair-rehearse-and-commission). Watch each lamp and verify both turn off. Complete the [physical checklist](hardware.md#physical-release-checklist) at home.

After the lamps pass their checks, an optional [device profile](#enable-an-optional-device) adds USB playback on any listed Pi, SPI glow on Pi 5, or both.

## Stage the development build

Prepare the Pi, clock, and two plugs using the [setup guide](service.md#prepare-the-pi-and-plugs). Start with a fresh installation. For an existing installation, use its original account, state, and config to disarm and verify shutdown. Disable and stop the unit, then follow the [upgrade procedure](service.md#upgrade-or-switch-profiles) to preserve the old application. Keep existing plug pairings and recovery state when keeping the same service account. The procedure for changing accounts archives the old state after shutdown.

For a fresh checkout on the Pi, fetch the current `main` branch and inspect its commit. If you already have a reviewed checkout, start in that project's root directory.

```sh
git clone --depth 1 --branch main https://github.com/jeffhudesman/vacation-ghost.git vacation-ghost-dev
cd vacation-ghost-dev
git log -1 --format='%H %s'
```

From the reviewed checkout, install that commit at a new `/opt/vacation-ghost` and record its identity. The config template is installed only on first setup.

```sh
(
  set -eu
  app=/opt/vacation-ghost
  if sudo test -e "$app"; then
    echo "Preserve the existing application using the upgrade guide first." >&2
    exit 1
  fi
  commit=$(git --no-replace-objects rev-parse --verify 'HEAD^{commit}')
  stage=$(mktemp -d)
  git --no-replace-objects archive --format=tar "$commit" > "$stage/source.tar"
  tar -xf "$stage/source.tar" -C "$stage"
  /usr/bin/python3 -I - "$stage" "$commit" <<'PY'
import json, sys, tomllib
from pathlib import Path
stage = Path(sys.argv[1])
version = tomllib.loads((stage / "pyproject.toml").read_text())["project"]["version"]
(stage / "BUILD.json").write_text(json.dumps({"version": version, "commit": sys.argv[2]}) + "\n")
PY
  sudo install -d -o root -g root -m 0755 "$app" "$app/vacation_ghost" "$app/deploy" "$app/tools"
  sudo install -o root -g root -m 0644 "$stage"/vacation_ghost/*.py "$stage/BUILD.json" "$app/vacation_ghost/"
  sudo install -o root -g root -m 0644 "$stage"/deploy/* "$app/deploy/"
  sudo install -o root -g root -m 0644 "$stage"/tools/pi_service.py "$stage"/tools/bench_record.py "$stage"/tools/generate_sounds.py "$app/tools/"
  sudo /usr/bin/python3 -I -m venv --without-pip "$app/.venv"
  if ! sudo test -d /etc/vacation-ghost; then
    sudo install -d -o root -g root -m 0700 /etc/vacation-ghost
  fi
  if ! sudo test -e /etc/vacation-ghost/vacation.toml; then
    sudo install -o root -g root -m 0600 "$stage/examples/vacation.lights-only.toml" /etc/vacation-ghost/vacation.toml
  fi
  cd "$app"
  "$app/.venv/bin/python" -Es -m vacation_ghost --version
)
```

[Enter your private settings](service.md#set-the-trip-and-plug-addresses), including a future departure. Follow [Use the lights-only profile](service.md#use-the-lights-only-profile) to create the account and grant it access to the config and private state. For two lamps alone, continue directly to [pairing and rehearsal](#pair-rehearse-and-commission).

## Enable an optional device

With the unit stopped and shutdown confirmed, replace the lights-only drop-in with the shared device-account profile. Its device policy allows ordinary system pseudo-devices and the hardware you select below. Leave unrelated local drop-ins in place and review the effective unit before starting.

```sh
(
  set -eu
  dropins=/etc/systemd/system/vacation-ghost.service.d
  sudo install -m 0644 /opt/vacation-ghost/deploy/10-device-user.conf "$dropins/"
  sudo rm -f "$dropins/lights-only.conf"
)
```

## USB playback

Install ALSA tools, connect the USB adapter, and find its ALSA card name, card number, and playback device number.

```sh
sudo apt-get install -y alsa-utils
aplay -l
```

Set the system ALSA default in `/etc/asound.conf` using the USB adapter's stable card name. For a card named `Device` with playback device `0`, use the following settings. If the file already configures other audio hardware, preserve those definitions and update its defaults.

```text
pcm.!default {
    type plug
    slave.pcm "hw:CARD=Device,DEV=0"
}
ctl.!default {
    type hw
    card "Device"
}
```

Set `sounds_dir = "/opt/vacation-ghost/sounds"` and a low starting `volume` such as `0.05` under `[hardware]`. Use the speaker's physical volume control during rehearsal.

```sh
(
  set -eu
  sudo install -d -o root -g vacation-ghost -m 0750 /opt/vacation-ghost/sounds
  sudo sh -c 'umask 022; exec /opt/vacation-ghost/.venv/bin/python -I /opt/vacation-ghost/tools/generate_sounds.py /opt/vacation-ghost/sounds'
  sudo usermod -a -G audio vacation-ghost
  sudo install -m 0644 /opt/vacation-ghost/deploy/20-usb-audio.conf /etc/systemd/system/vacation-ghost.service.d/
  sudoedit /etc/systemd/system/vacation-ghost.service.d/20-usb-audio.conf
)
```

Match the two `DeviceAllow` paths to the numbers on your Pi. For card 1, playback device 0, they are `/dev/snd/controlC1` and `/dev/snd/pcmC1D0p`. The unit allows that control node and playback stream. Group membership lets the same account run a foreground rehearsal or recovery. If card numbers change after a reboot or USB reconfiguration, update the allow-list and repeat rehearsal under the installed unit.

## Pi 5 SPI glow

Follow the [wiring guide](hardware.md#wire-the-optional-led-strip) with both power supplies disconnected. Dedicate SPI0 to the strip. Install the OS driver and enable SPI. Confirm the outputs are off and the scheduled unit disabled before rebooting.

```sh
sudo apt-get install -y python3-spidev
sudo raspi-config nonint do_spi 0
sudo reboot
```

Set `led_backend = "spi"` and your actual `led_count` (1–144) in the existing `[hardware]` section. Omit `led_pin` to use BCM GPIO10, physical pin 19. Set `volume = 0` for a glow-only setup. The Linux SPI node should belong to the `spi` group and allow that group read/write access.

```sh
(
  set -eu
  test -c /dev/spidev0.0
  sudo usermod -a -G spi vacation-ghost
  sudo -u vacation-ghost -g vacation-ghost test -r /dev/spidev0.0
  sudo -u vacation-ghost -g vacation-ghost test -w /dev/spidev0.0
  sudo /usr/bin/python3 -I -m venv --without-pip --system-site-packages /opt/vacation-ghost/.venv
  /opt/vacation-ghost/.venv/bin/python -I -c 'import spidev'
  sudo install -m 0644 /opt/vacation-ghost/deploy/20-spi-glow.conf /etc/systemd/system/vacation-ghost.service.d/
)
```

## Pair, rehearse, and commission

With the scheduled unit stopped and both lamps in view, install the unit and inspect its effective settings before pairing. All profiles should show `User=vacation-ghost`. For lights only, expect `PrivateDevices=yes` and no SPI or ALSA allowances. For an optional device profile, expect `PrivateDevices=no`, `DevicePolicy=closed`, and exactly the hardware allowances you selected: two ALSA nodes for audio, one SPI node for glow, or all three for both.

```sh
(
  set -eu
  app=/opt/vacation-ghost
  unit=vacation-ghost.service
  sudo install -m 0644 "$app/deploy/$unit" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemd-analyze verify "/etc/systemd/system/$unit"
  sudo systemctl show "$unit" -p User -p PrivateDevices -p DevicePolicy -p DeviceAllow -p SupplementaryGroups
)
```

When the merged settings match your build, run the read-only plug checks and watch the rehearsal.

```sh
(
  set -eu
  app=/opt/vacation-ghost
  cd "$app"
  vg() {
    sudo -u vacation-ghost -g vacation-ghost "$app/.venv/bin/python" -Es -m vacation_ghost \
      --config /etc/vacation-ghost/vacation.toml "$@"
  }
  vg doctor
  vg pair
  rehearsal=$(sudo -u vacation-ghost -g vacation-ghost mktemp -d /var/lib/vacation-ghost/rehearsal.XXXXXX)
  printf 'Rehearsal state: %s\n' "$rehearsal"
  vg rehearse --state-dir "$rehearsal"
)
```

`doctor` reads the reported auto-on, auto-off, and power-restore settings and compares each timer to the longest planned run for its room. Confirm timer and power-loss behavior independently on both plugs. Pair while the configured addresses and rooms still match your observed devices. After a failed rehearsal, preserve its printed state directory and [recover with the same account](service.md#stop-recover-or-change-trips).

Once the rehearsal passes, commission a short future test trip with `python3 -I /opt/vacation-ghost/tools/pi_service.py commission`. The helper checks that a fresh service invocation runs as `vacation-ghost` and reaches a healthy, idle state before enabling startup at boot. Observe a scheduled lamp scene and verify recovery after reboot. Record relay readings and the observations for any fitted peripherals on the [physical checklist](hardware.md#physical-release-checklist). After a profile or permissions change, repeat these checks before relying on the setup.

## Remove an optional device

Disarm the trip and confirm shutdown with the original config and account, then disable and stop the unit. Set `volume = 0` to retire audio or `led_count = 0` to retire glow. Run the corresponding line for each device you retire. These commands remove the project drop-in and the group membership added by this guide.

```sh
# Retire USB audio.
sudo rm -f /etc/systemd/system/vacation-ghost.service.d/20-usb-audio.conf && sudo gpasswd -d vacation-ghost audio
```

```sh
# Retire SPI glow.
sudo rm -f /etc/systemd/system/vacation-ghost.service.d/20-spi-glow.conf && sudo gpasswd -d vacation-ghost spi
```

If you retire both, restore the lights-only configuration.

```sh
(
  set -eu
  dropins=/etc/systemd/system/vacation-ghost.service.d
  sudo install -m 0644 /opt/vacation-ghost/deploy/lights-only.conf "$dropins/"
  sudo rm -f "$dropins/10-device-user.conf"
)
```

Run `sudo systemctl daemon-reload`, then repeat the [effective profile checks and commissioning](#pair-rehearse-and-commission) before enabling boot startup again.
