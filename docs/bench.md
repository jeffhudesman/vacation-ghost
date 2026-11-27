# Record a two-lamp bench run

Use a fictional trip and two lamps you can watch. The existing [Pi 5 result](hardware.md#raspberry-pi-5-simulator-test) covers the Preview 2 simulator. This run checks the real plugs, lamps and installed service.

1. Create the record below from a development checkout. Enter the release commit, Pi OS image, service profile and exact plug models and firmware.
2. Follow the [lights-only setup](service.md#lights-only-quick-start). Keep the paper or local Markdown record beside the lamps. During rehearsal, record which lamp actually lit in each step.
3. Exercise the unplug, network interruption, reboot, manual disarm and scheduled shutdown cases in the record while someone is present. Keep the original configuration and state in place through recovery. Record the times you observed and whether both lamps went dark.
4. Compare your observations with the morning note and final report. Record failed and untested cases explicitly. Share a redacted copy only after reviewing it.

From the checkout, create a private record in a new file.

```sh
python3 tools/bench_record.py "$HOME/vacation-ghost-bench.md"
```

After a hardware-mode trip, the service account can create a separate record with selected status fields from its reports. Choose a new output filename for each trip. The following command assumes the checkout is readable by the service account and the lights-only profile uses its standard private state path.

```sh
sudo -u vacation-ghost python3 tools/bench_record.py \
  /var/lib/vacation-ghost/bench-controller.md \
  --state-dir /var/lib/vacation-ghost/current
```

For a development rehearsal, add `--rehearsal /path/to/rehearsal.json` to include the outcome and number of distinct lamps confirmed on. Preview 2 predates that file.

The tool copies fixed status flags and the device fault count. It omits trip identifiers, dates, addresses, room names and free-form messages. A hardware report can confirm a relay readback. The observer supplies physical outcomes for the lamps, strip and speaker. [Full physical checklist](hardware.md#physical-release-checklist)
