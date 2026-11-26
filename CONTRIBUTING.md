# Contributing to Vacation Ghost

## Run the tests and simulator

From a Git checkout, use Python 3.11 or later and the system time-zone database. The simulator uses in-memory outputs, and the tests use simulated devices to check hardware behavior.

```sh
python3 -m unittest discover -s tests -v
demo_dir=$(mktemp -d /tmp/vacation-ghost-demo.XXXXXX)
python3 -m vacation_ghost --config examples/vacation.example.toml simulate --state-dir "$demo_dir"
cat "$demo_dir/trip.json"
```

The example trip is fictional. Use temporary directories and fictional dates in tests and examples. Store private configs and reports outside the checkout or in its ignored `.local/` directory. Keep actual trip dates, household coordinates, device addresses, and generated reports out of commits, issues, and release artifacts.

## Repository layout

| Path | Purpose |
| --- | --- |
| [vacation_ghost/](vacation_ghost/) | The scheduler builds local-time events in `schedule.py`. The controller in `engine.py` runs them through the adapters in `outputs.py`. The report store in `reports.py` writes activity and summaries. |
| [examples/](examples/) and [deploy/](deploy/) | Example configs, the systemd unit, and the lights-only service profile. |
| [tests/](tests/) | Python tests for scheduling, reports, outputs, and recovery. |
| [site/](site/) and [tools/check_site.py](tools/check_site.py) | The static project website and its checks. |

## Before opening a pull request

- **Simulator or application changes.** Run the test suite and replay the fictional trip above. Call out changes to config keys and report fields because local consumers may depend on them.
- **Website or setup instructions.** Run the website checks and visit the changed page as a first-time reader. [Website preview and checks](#preview-and-check-the-website)
- **Device behavior.** Use fake outputs to test the normal path and relevant failures. For a physical test, record the Pi, OS, plug firmware, and output devices and follow the [bench checklist](docs/hardware.md#physical-release-checklist). State which hardware checks are still pending.

Describe the behavior change and the checks you ran in the pull request. Documentation fixes and bench results are welcome.

## Check the package

| Starting point | Contents |
| --- | --- |
| Git checkout | Application, tests, setup files, and the complete project website. Use this for the Pi service installation. |
| Source distribution (`.tar.gz`) | Application source, tests, hardware guides, service files, example configs, and the sound generator. |
| Wheel (`.whl`) | Linux `vacation-ghost` CLI and the MIT license. Use an example config from the source distribution or checkout. |

To install the Linux CLI from a checkout, run `python3 -m pip install .` in a virtual environment.

Builds from `main` carry a `.dev` package version. Tagged previews carry the version set for that release.

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

## Preview and check the website

Run the preview from the checkout and open [http://127.0.0.1:4173](http://127.0.0.1:4173) on the same machine.

```sh
python3 -m http.server --bind 127.0.0.1 --directory site 4173
```

Fetch the preview tags and check the site’s links, assets, and simulator instructions with Node.js available.

```sh
git fetch origin --tags
python3 tools/check_site.py
```

For browser checks, install `playwright==1.62.0` in your development environment and run `python3 tools/check_site.py --browser` with Chromium or Chrome installed.

## Website deployment

The [Deploy website workflow](.github/workflows/pages.yml) runs when a maintainer dispatches it on `main`. It validates the site and pinned simulator before uploading the Pages artifact.

The project uses the [MIT License](LICENSE). Keep third-party code and custom WAV files under terms that allow redistribution before adding them to the project.
