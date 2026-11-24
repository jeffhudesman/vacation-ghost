# Run Vacation Ghost at boot

The systemd service runs on the Pi, resumes after a reboot, and exits at the end of the configured trip. It restarts the controller if it exits with an error. Set up the [hardware](hardware.md) and test on site before leaving. The Pi needs an accurate clock after a power loss. If your Pi lacks a battery-backed clock, confirm its network time service can sync in your deployment, for example with a trusted local NTP server.

## Install the service

Run from the project checkout on Raspberry Pi OS. The unit runs as root for access to PWM/DMA on Pi models that require it. These commands copy the program into a root-owned directory and create a private system config. Keep trip dates and LAN addresses in the system config.

```sh
sudo apt-get update
sudo apt-get install -y python3-venv
sudo install -d -m 0755 /opt/vacation-ghost/vacation_ghost /opt/vacation-ghost/tools
sudo install -m 0644 vacation_ghost/*.py /opt/vacation-ghost/vacation_ghost/
sudo install -m 0644 tools/generate_sounds.py /opt/vacation-ghost/tools/
sudo python3 -m venv /opt/vacation-ghost/.venv

sudo install -d -m 0700 /etc/vacation-ghost /var/lib/vacation-ghost
if ! sudo test -e /etc/vacation-ghost/vacation.toml; then
  sudo install -m 0600 examples/vacation.example.toml /etc/vacation-ghost/vacation.toml
fi
sudoedit /etc/vacation-ghost/vacation.toml
```

Set the local trip dates, timezone, rooms, and plug addresses. The lights-only configuration uses `[hardware]` with `led_count = 0`, `volume = 0`, and a `[hardware.plugs]` table with both rooms. Leave `sounds_dir` out for lights-only use. For glow, set the count and pin and install the LED driver.

```sh
sudo /opt/vacation-ghost/.venv/bin/python -m pip install rpi_ws281x
```

For sound, set `sounds_dir = "/opt/vacation-ghost/sounds"` and set `volume` low. Install ALSA tools, make the USB audio adapter the default for the service account, and generate the cues.

```sh
sudo apt-get install -y alsa-utils
sudo /opt/vacation-ghost/.venv/bin/python /opt/vacation-ghost/tools/generate_sounds.py /opt/vacation-ghost/sounds
```

The application contacts the configured plugs on the LAN. It exposes no network service. Internet access is needed for any packages installed above. The service’s root-owned state directory keeps the reports local and private.

## Check and start

Run the configuration check and a short physical rehearsal. `doctor` checks each plug through the read-only `Switch.GetStatus` RPC. `rehearse` ignores trip dates and briefly drives the connected outputs. Run it while home with the scheduled service stopped. The rehearsal uses a separate directory and shares the device reservation, so it cannot take over an active or unresolved trip.

```sh
cd /opt/vacation-ghost
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml doctor
rehearsal_dir=$(sudo mktemp -d /var/lib/vacation-ghost/rehearsal.XXXXXX)
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml rehearse --state-dir "$rehearsal_dir"
```

From the project checkout, install the unit and start it for the trip.

```sh
sudo install -m 0644 deploy/vacation-ghost.service /etc/systemd/system/vacation-ghost.service
sudo systemd-analyze verify /etc/systemd/system/vacation-ghost.service
sudo systemctl daemon-reload
sudo systemctl enable --now vacation-ghost.service
sudo systemctl status vacation-ghost.service
sudo journalctl -u vacation-ghost.service -n 40
```

The unit uses `/var/lib/vacation-ghost/current` for the active trip and `/var/lib/vacation-ghost/device` for the shared device reservation. Keep both across restarts so the process can finish shutdown and preserve report history. A healthy service can wait for a future trip start. After the last report, a successful exit leaves the unit inactive until it’s started again or the Pi reboots. Check `current/latest.json` for the latest `updated_at`, `shutdown_unconfirmed`, and device faults.

## Stop or change trips

Use `disarm` with the same config and state directory to end the trip and turn outputs off, including while the service is running. Disarm persists across a restart with that state directory. Disable the unit to prevent it from starting at boot.

```sh
cd /opt/vacation-ghost
sudo .venv/bin/python -m vacation_ghost --config /etc/vacation-ghost/vacation.toml disarm --state-dir /var/lib/vacation-ghost/current
sudo cat /var/lib/vacation-ghost/current/latest.json
```

If the command queues a request for the running service, wait for the service to exit and recheck `latest.json`. With the service stopped, check that `shutdown_unconfirmed` is `false`. If shutdown is unconfirmed, check the lamps directly, use the plugs’ own switches if needed, and fix the connection. Retry `disarm` with the same configuration and state directory. When a rehearsal ends with unconfirmed shutdown, rerun `rehearse` with its original directory. Preserve the old plug addresses and state until shutdown is confirmed.

```sh
sudo systemctl disable --now vacation-ghost.service
```

To prepare a new trip after confirmed shutdown and service stop, archive `/var/lib/vacation-ghost/current` under a fresh name. Set new trip dates and run the checks before enabling the service again. The service creates the next trip’s `current` directory.
