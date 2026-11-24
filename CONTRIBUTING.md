# Contributing to Vacation Ghost

Run the simulator and tests with Python 3.11 or later and the system time-zone database. The usual development workflow needs no smart plugs, audio devices, GPIO, or pip dependencies.

```sh
python3 -m unittest discover -s tests -v
demo_dir=$(mktemp -d /tmp/vacation-ghost-demo.XXXXXX)
python3 -m vacation_ghost --config examples/vacation.example.toml simulate --state-dir "$demo_dir"
cat "$demo_dir/trip.json"
```

The example trip is fictional. Use temporary directories and fictional dates in tests and examples. Store private configs and reports outside the checkout or in its ignored `.local/` directory. Keep actual trip dates, household coordinates, device addresses, and generated reports out of commits, issues, and release artifacts.

The scheduler builds local-time events in `vacation_ghost/schedule.py`. The controller in `vacation_ghost/engine.py` runs those events through the adapters in `vacation_ghost/outputs.py`. The report store in `vacation_ghost/reports.py` writes activity and summaries.

## Before opening a pull request

- **Simulator or application changes.** Run the test suite and replay the fictional trip above. Call out changes to config keys and report fields because local consumers may depend on them.
- **Website or setup instructions.** Fetch the preview tags with `git fetch origin --tags`, then run `python3 tools/check_site.py` from the checkout with Node.js available. Check the changed page as a first-time visitor. For the browser checks, install `playwright==1.62.0` in your development environment and run `python3 tools/check_site.py --browser` with Chromium or Chrome installed.
- **Device behavior.** Use fake outputs to test the normal path and relevant failures. For a physical test, record the Pi, OS, plug firmware, and output devices and follow the [bench checklist](docs/hardware.md#physical-release-checklist). State which hardware checks are still pending.

Describe the behavior change and the checks you ran in the pull request. Documentation fixes and bench results are welcome.

## Check the package

Use Python 3.11 or later and install the CI build frontend with `python3 -m pip install build==1.3.0` in a development virtual environment. Run this from a Git checkout at the commit you intend to package. It builds the source distribution and wheel from the committed files, then runs the installed CLI in a fresh environment.

```sh
(
  set -eu
  release_dir=$(mktemp -d /tmp/vacation-ghost-package.XXXXXX)
  mkdir "$release_dir/source"
  git --no-replace-objects archive --format=tar HEAD > "$release_dir/source.tar"
  tar -xf "$release_dir/source.tar" -C "$release_dir/source"
  python3 -m build --outdir "$release_dir/dist" "$release_dir/source"
  python3 -m venv "$release_dir/venv"
  "$release_dir/venv/bin/python" -m pip install --no-index --no-deps "$release_dir"/dist/*.whl
  "$release_dir/venv/bin/vacation-ghost" \
    --config "$release_dir/source/examples/vacation.example.toml" simulate \
    --state-dir "$release_dir/state"
  cat "$release_dir/state/trip.json"
)
```

The package CI job also checks the source distribution's expected files and verifies that a synthetic private config stays out. Use the [physical release checklist](docs/hardware.md#physical-release-checklist) before calling a Pi configuration verified.

## Website deployment

The [Deploy website workflow](.github/workflows/pages.yml) runs when a maintainer dispatches it on `main`. It validates the site and pinned simulator before uploading the Pages artifact.

The project uses the [MIT License](LICENSE). Keep third-party code and custom WAV files under terms that allow redistribution before adding them to the project.
