# Contributing to Vacation Ghost

Run the simulator and tests with Python 3.11 or later and the system time-zone database. The usual development workflow needs no smart plugs, audio devices, GPIO, or pip dependencies.

```sh
python3 -m unittest discover -s tests -v
demo_dir=$(mktemp -d /tmp/vacation-ghost-demo.XXXXXX)
python3 -m vacation_ghost --config examples/vacation.example.toml simulate --state-dir "$demo_dir"
cat "$demo_dir/trip.json"
```

The example trip is fictional. Use temporary directories and fictional dates in tests and examples. Keep actual trip dates, household coordinates, device addresses, and generated reports out of commits and issues.

The scheduler builds local-time events in `vacation_ghost/schedule.py`. The controller in `vacation_ghost/engine.py` runs those events through the adapters in `vacation_ghost/outputs.py`. The report store in `vacation_ghost/reports.py` writes activity and summaries. For device changes, use the simulator or fake outputs to verify normal operation and failures where practical. Run the [physical checklist](docs/hardware.md#physical-release-checklist) when changing hardware behavior and record which Pi, OS, plugs, and output devices you used.

When opening a pull request, describe the behavior change and the checks you ran. Call out changes to config keys or report fields because local consumers may depend on them.

The project uses the [MIT License](LICENSE). Keep third-party code and custom WAV files under terms that allow redistribution before adding them to the project.
