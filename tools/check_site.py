"""Check the project website and the copyable simulator example."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
REPOSITORY = "https://github.com/jeffhudesman/vacation-ghost"
RAW_REPOSITORY = "https://raw.githubusercontent.com/jeffhudesman/vacation-ghost"
DEMO_SCENES = {
    "Sunset": ("kitchen", "evening", "lamp", "kitchen"),
    "After dinner": ("living", "evening", "lamp", "living"),
    "TV time": ("tv", "evening", "sound", "tv"),
    "Kitchen break": ("break", "evening", "sound", "cabinet"),
    "Late TV": ("drama", "evening", "glow", "drama"),
    "Bedtime": ("bedtime", "evening", "lamp", ""),
    "Morning kitchen": ("morning", "dawn", "sound", "kettle"),
    "Morning note": ("report", "dawn", "report", ""),
}
DEMO_AUDIO = ("assets/demo-tv.mp3", "assets/demo-cabinet.mp3", "assets/demo-kettle.mp3")
SCENE_DELAY = 3000
SCENE_SECONDS = SCENE_DELAY / 1000
DEMO_DURATION = (len(DEMO_SCENES) - 1) * SCENE_SECONDS
FRAME_TOLERANCE_SECONDS = 0.02


class PageSource(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = Counter()
        self.links = []
        self.text = {}
        self._capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if identifier := attrs.get("id"):
            self.ids[identifier] += 1
            if tag in ("code", "blockquote"):
                self._capture = (tag, identifier)
                self.text[identifier] = ""
        self.links.extend(attrs[name] for name in ("href", "src") if name in attrs)
        self.links.extend(item.strip().split()[0] for item in attrs.get("srcset", "").split(",") if item.strip())
        if attrs.get("property") == "og:image":
            self.links.append(attrs["content"])

    def handle_data(self, data):
        if self._capture:
            self.text[self._capture[1]] += data

    def handle_endtag(self, tag):
        if self._capture and tag == self._capture[0]:
            self._capture = None


def git(*args):
    return subprocess.check_output(["git", "-C", ROOT, *args], text=True)


def check_pi_setup(snippet, revision):
    unit = ConfigParser(interpolation=None)
    unit.read_string(git("show", f"{revision}:deploy/vacation-ghost.service"))
    unit.read_string(git("show", f"{revision}:deploy/lights-only.conf"))
    service = unit["Service"]
    service_command = shlex.split(service["ExecStart"])
    doctor_command = [
        "sudo", "-u", service["User"], "-g", service["Group"],
        *service_command[:service_command.index("run")], "doctor",
    ]
    lines = snippet.replace("\\\n", "").strip().splitlines()
    assert [shlex.split(line) for line in lines] == [
        ["("], ["set", "-eu"], ["cd", service["WorkingDirectory"]], doctor_command, [")"],
    ], "Pi doctor command differs from the pinned lights-only service"


def check_source(page):
    assert all(count == 1 for count in page.ids.values()), "Duplicate HTML IDs"
    revision = page.text["preview-commit"].strip()
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "Expected a full preview commit ID"
    release_links = [url for url in page.links if url.startswith(f"{REPOSITORY}/releases/tag/")]
    tag_url, = release_links
    tag = tag_url.rsplit("/", 1)[-1]
    assert git("rev-parse", f"refs/tags/{tag}^{{commit}}").strip() == revision, "Preview tag and commit differ"

    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assets = re.findall(r'url\("([^"]+)"\)', css)
    for cue in DEMO_AUDIO:
        assert (SITE / cue).is_file() and (SITE / cue).stat().st_size > 0, cue
    for url in [*page.links, *assets]:
        parts = urlsplit(url)
        if not parts.netloc:
            assert (SITE / parts.path).is_file() if parts.path else parts.fragment in page.ids, url
        elif parts.netloc == "vacationghost.com" and parts.path.startswith("/assets/"):
            assert (SITE / parts.path.lstrip("/")).is_file(), url
        elif url.startswith(f"{REPOSITORY}/blob/"):
            ref, path = parts.path.split("/blob/", 1)[1].split("/", 1)
            if ref == "main":
                assert path in {"LICENSE", "CONTRIBUTING.md"} or (
                    path == "docs/hardware.md"
                    and parts.fragment in {"optional-glow-and-sound", "raspberry-pi-5-simulator-test"}
                ), url
                content = (ROOT / path).read_text(encoding="utf-8")
            else:
                assert ref == revision, url
                content = git("show", f"{ref}:{path}")
            if parts.fragment:
                headings = re.findall(r"^#+ (.+)$", content, re.MULTILINE)
                anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                assert parts.fragment in anchors, url
        elif url.startswith(f"{RAW_REPOSITORY}/"):
            ref, path = url.removeprefix(f"{RAW_REPOSITORY}/").split("/", 1)
            assert ref == revision, url
            git("cat-file", "-e", f"{ref}:{path}")

    subprocess.run(["node", "--check", SITE / "site.js"], check=True)
    for name in ("setup-code-simulator", "setup-code-pi"):
        subprocess.run(["sh", "-n"], input=page.text[name], text=True, check=True)
    assert f"git checkout --quiet --detach {revision}\n" in page.text["setup-code-simulator"]
    check_pi_setup(page.text["setup-code-pi"], revision)


def sample_schedule(checkout, report):
    # Run the actual pinned scheduler from the checkout created by the website command.
    program = """
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from vacation_ghost.config import load_config
from vacation_ghost.engine import Routine
from vacation_ghost.reports import ReportStore, _clock_time

config = load_config(Path("examples/vacation.example.toml"))
routine = Routine(config)
night, morning = map(date.fromisoformat, sys.argv[1:])
note_time = datetime.combine(morning, time(8), tzinfo=config.timezone)

with TemporaryDirectory() as directory:
    state = Path(directory)
    reports = ReportStore(state, config.start, config.end, dry_run=True)
    note = state / "daily" / f"{morning}.json"
    reports.publish_due(note_time - timedelta(seconds=1))
    assert not note.exists()
    reports.publish_due(note_time)
    assert note.exists()

def sample(at, kind, value):
    lamp, glow, cue = routine.desired(at, at - timedelta(microseconds=1))
    return [kind, value, at.strftime("%H:%M"), _clock_time(at), lamp or "none", glow or "none", cue or "none"]

def samples(day):
    return [sample(event.at, event.kind, event.value)
            for event in routine.day(day).events if routine.active(event.at)]

print(json.dumps({"evening": samples(night), "dawn": [*samples(morning), sample(note_time, "report", "")]}))
"""
    return json.loads(subprocess.check_output(
        [sys.executable, "-c", program, report["night_of"], report["delivery_date"]],
        cwd=checkout, text=True, timeout=15,
    ))


def check_simulator(page):
    with TemporaryDirectory(prefix="vacation-ghost-website-") as temp:
        # Exercise the published command using the already fetched Git objects.
        env = dict(os.environ, TMPDIR=temp, GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0=f"url.{ROOT.as_uri()}.insteadOf",
                   GIT_CONFIG_VALUE_0=f"{REPOSITORY}.git")
        result = subprocess.run(["sh"], input=page.text["setup-code-simulator"], text=True,
                                capture_output=True, cwd=temp, env=env, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        _, payload = result.stdout.split("\n", 1)
        report = json.loads(payload)
        example = page.text["sample-report-text"].strip("“” \n")
        assert report["dry_run"] and report["lamp_seconds"] == 11460, report
        assert example in report["message"], (example, report["message"])
        summary = json.loads(page.text["sample-report-json"])
        message = summary.pop("message")
        assert report["message"] == message, (message, report["message"])
        assert all(report[key] == value for key, value in summary.items()), (summary, report)
        checkout, = Path(temp).glob("*/vacation-ghost")
        return sample_schedule(checkout, report)


class SiteHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def assert_scene(page, group, label):
    scene = page.locator(".routine-scene")
    name, *_ = DEMO_SCENES[label]
    assert scene.get_attribute("data-scene") == name, label
    assert page.locator("#scene-name").inner_text() == label
    assert group.locator('[aria-pressed="true"]').count() == 1, label
    assert group.get_by_role("button", name=label, exact=True).get_attribute("aria-pressed") == "true", label


def assert_scheduled_state(page, label, schedule):
    _, period, kind, value = DEMO_SCENES[label]
    clock, clock_text, lamp, glow, cue = next(
        state for event_kind, event_value, *state in schedule[period]
        if (event_kind, event_value) == (kind, value)
    )
    scene = page.locator(".routine-scene")
    for attribute, expected in (("lamp", lamp), ("glow", glow), ("period", period), ("cue", cue)):
        assert scene.get_attribute(f"data-{attribute}") == expected, (label, attribute, expected)
    scene_time = page.locator("#scene-time")
    assert scene_time.get_attribute("datetime") == clock, label
    assert scene_time.inner_text() == clock_text, label


def assert_progress(progress, seconds, *, tolerance=0):
    actual = progress.evaluate("progress => progress.value")
    assert abs(actual - seconds) <= tolerance, (actual, seconds, tolerance)
    return actual


def assert_window_lamps(page, kitchen, living):
    actual = page.locator(".routine-scene").evaluate("""scene => [
        Number(getComputedStyle(scene.querySelector('.scene-window-kitchen'), '::before').opacity),
        Number(getComputedStyle(scene.querySelector('.scene-window-living'), '::before').opacity),
        Number(getComputedStyle(scene.querySelector('.scene-window-living'), '::after').opacity),
    ]""")
    assert actual == [kitchen, living, 0], actual


def assert_sample_report(page, visible):
    report = page.locator("#sample-report")
    assert report.is_visible() == visible
    if visible:
        assert report.locator("figcaption").inner_text() == "Sample simulator report"
        assert report.locator("p").inner_text() == "Your trip reports stay on your Pi."


def assert_sound_state(page, enabled):
    sound = page.get_by_role("button", name="Sound", exact=True)
    assert sound.get_attribute("id") == "scene-sound"
    assert sound.get_attribute("aria-pressed") == str(enabled).lower()
    state = "on" if enabled else "off"
    assert " ".join(sound.inner_text().split()) == f"Sound {state}"
    indicator = sound.locator("#scene-sound-state")
    assert indicator.is_visible()
    assert indicator.inner_text() == state
    assert indicator.get_attribute("aria-hidden") == "true"


def assert_demo_ready(page):
    scene = page.locator(".routine-scene")
    assert scene.get_attribute("data-scene") == "ready"
    assert scene.get_attribute("data-lamp") == "none"
    assert scene.get_attribute("data-glow") == "none"
    assert page.locator("#scene-name").inner_text() == "Dusk"
    assert_window_lamps(page, 0, 0)
    assert page.locator('[data-scene-button][aria-pressed="true"]').count() == 0
    assert_progress(page.locator("#scene-progress"), 0)
    assert_sample_report(page, False)


def check_scene_selection(page, width, height, schedule):
    page.locator('.hero a[href="#sample-evening"]').click()
    assert_demo_ready(page)
    picker = page.locator(".routine-picker")
    assert picker.locator("summary").inner_text() == "Jump to a scene", width
    assert picker.evaluate("picker => picker.open") == (width > 480), width
    play = page.get_by_role("button", name="Play demo", exact=True)
    initial = play.bounding_box()
    assert initial is not None and 0 <= initial["y"] < height, width
    image = page.locator(".routine-scene").bounding_box()
    caption = page.locator(".scene-caption").bounding_box()
    assert image["y"] + image["height"] <= caption["y"] < initial["y"], width
    x, y = initial["x"] + initial["width"] / 2, initial["y"] + initial["height"] / 2
    page.mouse.click(x, y)
    playing = page.get_by_role("button", name="Pause", exact=True)
    assert playing.bounding_box() == initial, width
    assert page.locator(".routine-scene").get_attribute("data-lamp") == "kitchen", width
    page.mouse.click(x, y)
    paused = page.get_by_role("button", name="Resume", exact=True)
    assert paused.bounding_box() == initial, width
    steps = page.get_by_role("group", name="Manual playback")
    previous = steps.get_by_role("button", name="Previous scene")
    next_scene = steps.get_by_role("button", name="Next scene")
    assert previous.is_disabled()
    frame = page.locator(".routine-frame")
    initial_frame = frame.bounding_box()
    step_controls = steps.bounding_box()
    assert initial_frame["y"] >= 0 and initial_frame["y"] + initial_frame["height"] <= height, width
    for label in list(DEMO_SCENES)[1:]:
        next_scene.click()
        assert_scheduled_state(page, label, schedule)
        assert steps.bounding_box() == step_controls, (width, label)
        assert frame.bounding_box() == initial_frame, (width, label)
    assert next_scene.is_disabled()
    assert_sample_report(page, True)
    assert page.locator("#sample-report").bounding_box() == initial_frame, width
    previous.click()
    assert_scheduled_state(page, "Morning kitchen", schedule)
    assert_sample_report(page, False)

    if width <= 480:
        picker.locator("summary").click()
        assert picker.evaluate("picker => picker.open"), width
    group = page.get_by_role("group", name="Sample scenes")
    first_scene = group.get_by_role("button", name="Sunset", exact=True)
    first_scene.scroll_into_view_if_needed()
    assert 0 <= first_scene.bounding_box()["y"] < height, width
    assert_sound_state(page, False)
    assert page.locator("#scene-audio").get_attribute("src") is None, width
    assert_scene(page, group, "Morning kitchen")
    progress = page.locator("#scene-progress")
    for index, label in enumerate(DEMO_SCENES):
        group.get_by_role("button", name=label, exact=True).click()
        assert_scene(page, group, label)
        assert_scheduled_state(page, label, schedule)
        assert_progress(progress, index * SCENE_SECONDS)
        frame_box = frame.bounding_box()
        assert frame_box["y"] >= 0 and frame_box["y"] + frame_box["height"] <= height, (width, label)
        assert_sample_report(page, label == "Morning note")
        if label == "TV time":
            assert page.locator(".routine-scene").evaluate("""scene =>
                scene.getAnimations({ subtree: true }).length === 0
            """), width
    card = page.locator(".routine-card").bounding_box()
    controls = [
        *group.get_by_role("button").all(),
        page.locator("#scene-play"),
        page.locator("#scene-restart"),
        page.locator("#scene-sound"),
        previous,
        next_scene,
    ]
    for control in controls:
        box = control.bounding_box()
        assert box is not None, (width, control)
        assert card["x"] <= box["x"] <= box["x"] + box["width"] <= card["x"] + card["width"], (width, control)
    demo = page.locator(".routine-card")
    parts_link = demo.get_by_role("link", name="See what you need", exact=True)
    simulator_link = demo.get_by_role("link", name="Try the Linux simulator", exact=True)
    assert parts_link.get_attribute("href") == "#parts", width
    assert simulator_link.get_attribute("href") == "#setup-panel-simulator", width
    note = page.locator("#sample-report").bounding_box()
    for link in (parts_link, simulator_link):
        assert link.bounding_box()["y"] >= note["y"] + note["height"], width
    simulator_link.click()
    assert page.evaluate("location.hash") == "#setup-panel-simulator", width
    assert page.locator("#setup-panel-simulator").is_visible(), width
    parts_link.click()
    assert page.evaluate("location.hash") == "#parts", width


def open_clocked_demo(page, url):
    page.bring_to_front()
    start = datetime(2030, 6, 10, tzinfo=UTC)
    page.clock.install(time=start)
    page.clock.pause_at(start + timedelta(minutes=1))
    page.goto(url)
    page.locator(".routine-card").evaluate("card => card.scrollIntoView({ block: 'start', behavior: 'instant' })")


def play_first_fade(page):
    # Seek the CSS transition directly while the demo's JavaScript clock is held.
    fade = page.evaluate("""() => {
        const kitchen = document.querySelector('.scene-window-kitchen');
        const opacity = () => Number(getComputedStyle(kitchen, '::before').opacity);
        const before = opacity();
        document.querySelector('#scene-play').click();
        const started = opacity();
        const transition = kitchen.getAnimations({ subtree: true }).find(animation =>
            animation.transitionProperty === 'opacity');
        if (!transition) return { before, started };
        const duration = transition.effect.getComputedTiming().duration;
        transition.pause();
        transition.currentTime = duration / 2;
        const middle = opacity();
        transition.finish();
        return { before, started, duration, middle, after: opacity() };
    }""")
    assert fade["before"] == fade["started"] == 0, fade
    assert fade.get("duration") == 550, fade
    assert 0 < fade["middle"] < fade["after"] == 1, fade


def check_demo_playback(page, url):
    # Mock only playback calls so the test does not need an audio device.
    page.add_init_script("""
        window.demoAudioCalls = { play: [], pause: 0 };
        const pause = HTMLMediaElement.prototype.pause;
        HTMLMediaElement.prototype.pause = function () {
            window.demoAudioCalls.pause += 1;
            return pause.call(this);
        };
        HTMLMediaElement.prototype.play = function () {
            window.demoAudioCalls.play.push(this.getAttribute("src"));
            return Promise.resolve();
        };
    """)
    open_clocked_demo(page, url)
    group = page.get_by_role("group", name="Sample scenes")
    play = page.locator("#scene-play")
    restart = page.locator("#scene-restart")
    sound = page.locator("#scene-sound")
    audio = page.locator("#scene-audio")
    progress = page.locator("#scene-progress")

    assert play.inner_text() == "Play demo"
    assert_demo_ready(page)
    assert progress.evaluate("progress => progress.max") == DEMO_DURATION
    assert_sound_state(page, False)
    sound.dispatch_event("click")
    assert_sound_state(page, True)
    assert audio.get_attribute("src") is None
    assert page.evaluate("window.demoAudioCalls.play") == []
    sound.dispatch_event("click")
    assert_sound_state(page, False)
    play_first_fade(page)
    assert play.inner_text() == "Pause"
    assert_scene(page, group, "Sunset")
    assert_window_lamps(page, 1, 0)
    assert_sample_report(page, False)
    for seconds in (1, 2):
        page.clock.run_for(1000)
        assert_scene(page, group, "Sunset")
        assert_progress(progress, seconds, tolerance=FRAME_TOLERANCE_SECONDS)
    page.clock.run_for(SCENE_DELAY - 2000 - 1)
    assert_scene(page, group, "Sunset")
    page.clock.run_for(1)
    assert_scene(page, group, "After dinner")

    before_pause = 1200
    page.clock.run_for(before_pause)
    assert_progress(progress, 4.2, tolerance=FRAME_TOLERANCE_SECONDS)
    play.dispatch_event("click")
    assert play.inner_text() == "Resume"
    assert_sample_report(page, False)
    frozen = assert_progress(progress, 4.2, tolerance=1e-6)
    page.clock.run_for(2 * SCENE_DELAY)
    assert_scene(page, group, "After dinner")
    assert_progress(progress, frozen)

    play.dispatch_event("click")
    assert play.inner_text() == "Pause"
    assert_progress(progress, frozen)
    after_resume = 600
    page.clock.run_for(after_resume)
    assert_scene(page, group, "After dinner")
    assert_progress(progress, 4.8, tolerance=FRAME_TOLERANCE_SECONDS)
    page.clock.run_for(SCENE_DELAY - before_pause - after_resume - 1)
    assert_scene(page, group, "After dinner")
    page.clock.run_for(1)
    assert_scene(page, group, "TV time")
    assert_sound_state(page, False)
    assert audio.get_attribute("src") is None
    assert page.evaluate("window.demoAudioCalls.play") == []

    sound.dispatch_event("click")
    assert_sound_state(page, True)
    assert page.evaluate("window.demoAudioCalls.play") == [DEMO_AUDIO[0]]
    pauses = page.evaluate("window.demoAudioCalls.pause")
    sound.dispatch_event("click")
    assert_sound_state(page, False)
    assert page.evaluate("window.demoAudioCalls.pause") > pauses

    for label in ("Kitchen break", "Late TV", "Bedtime", "Morning kitchen", "Morning note"):
        page.clock.run_for(SCENE_DELAY)
        assert_scene(page, group, label)
    assert page.evaluate("window.demoAudioCalls.play") == [DEMO_AUDIO[0]]
    assert play.inner_text() == "Complete"
    assert play.is_disabled()
    assert_sample_report(page, True)
    assert_progress(progress, DEMO_DURATION)
    assert page.locator("#sample-report").get_attribute("data-current") == "true"
    page.clock.run_for(SCENE_DELAY)
    assert_scene(page, group, "Morning note")
    assert_progress(progress, DEMO_DURATION)

    restart.dispatch_event("click")
    assert play.inner_text() == "Pause"
    assert_scene(page, group, "Sunset")
    assert_progress(progress, 0)
    assert_sample_report(page, False)
    assert page.locator("#sample-report").get_attribute("data-current") is None
    page.clock.run_for(int(DEMO_DURATION * 1000) - 1)
    assert_scene(page, group, "Morning kitchen")
    assert_sample_report(page, False)
    page.clock.run_for(1)
    assert_scene(page, group, "Morning note")
    assert_progress(progress, DEMO_DURATION)
    assert_sample_report(page, True)

    restart.dispatch_event("click")
    group.get_by_role("button", name="Late TV").dispatch_event("click")
    assert play.inner_text() == "Resume"
    assert_sample_report(page, False)
    assert_progress(progress, 4 * SCENE_SECONDS)
    page.clock.run_for(2 * SCENE_DELAY)
    assert_scene(page, group, "Late TV")
    assert_progress(progress, 4 * SCENE_SECONDS)
    play.dispatch_event("click")
    page.clock.run_for(SCENE_DELAY)
    assert_scene(page, group, "Bedtime")

    group.get_by_role("button", name="TV time").dispatch_event("click")
    sound.dispatch_event("click")
    assert_sound_state(page, True)
    group.get_by_role("button", name="Kitchen break").dispatch_event("click")
    group.get_by_role("button", name="Morning kitchen").dispatch_event("click")
    assert page.evaluate("window.demoAudioCalls.play") == [DEMO_AUDIO[0], *DEMO_AUDIO]


def check_demo_selected_start(page, url):
    open_clocked_demo(page, url)
    group = page.get_by_role("group", name="Sample scenes")
    play = page.locator("#scene-play")
    restart = page.locator("#scene-restart")
    progress = page.locator("#scene-progress")

    group.get_by_role("button", name="Late TV").dispatch_event("click")
    assert play.inner_text() == "Play from here"
    play.dispatch_event("click")
    assert_scene(page, group, "Late TV")
    assert_progress(progress, 4 * SCENE_SECONDS)
    page.clock.run_for(SCENE_DELAY)
    assert_scene(page, group, "Bedtime")

    page.clock.run_for(1200)
    restart.dispatch_event("click")
    assert_scene(page, group, "Sunset")
    assert_progress(progress, 0)
    page.clock.run_for(SCENE_DELAY - 1)
    assert_scene(page, group, "Sunset")
    page.clock.run_for(1)
    assert_scene(page, group, "After dinner")


def check_demo_auto_pause(page, url):
    from playwright.sync_api import expect

    open_clocked_demo(page, url)
    card = page.locator(".routine-card")
    frame = page.locator(".routine-frame")
    group = page.get_by_role("group", name="Sample scenes")
    play = page.locator("#scene-play")
    progress = page.locator("#scene-progress")
    # Wait for a rendered onscreen entry before exercising the transition offscreen.
    frame.evaluate("""frame => {
        new IntersectionObserver(([entry]) => {
            frame.dataset.testIntersecting = String(entry.isIntersecting);
        }).observe(frame);
    }""")
    expect(frame).to_have_attribute("data-test-intersecting", "true", timeout=3000)
    play.dispatch_event("click")
    page.clock.run_for(1200)

    frame.evaluate("frame => window.scrollBy({ top: frame.getBoundingClientRect().bottom + 1, behavior: 'instant' })")
    assert card.evaluate("card => card.getBoundingClientRect().bottom > 0")
    expect(frame).to_have_attribute("data-test-intersecting", "false", timeout=3000)
    expect(play).to_have_text("Resume", timeout=3000)
    frozen = assert_progress(progress, 1.2, tolerance=1e-6)
    page.clock.run_for(2 * SCENE_DELAY)
    assert_scene(page, group, "Sunset")
    assert_progress(progress, frozen)

    card.evaluate("card => card.scrollIntoView({ block: 'start', behavior: 'instant' })")
    expect(frame).to_have_attribute("data-test-intersecting", "true", timeout=3000)
    page.clock.run_for(2 * SCENE_DELAY)
    assert play.inner_text() == "Resume"
    assert_scene(page, group, "Sunset")
    assert_progress(progress, frozen)
    play.dispatch_event("click")
    assert_progress(progress, frozen)
    page.clock.run_for(600)
    assert_progress(progress, 1.8, tolerance=FRAME_TOLERANCE_SECONDS)
    page.clock.run_for(SCENE_DELAY - 1200 - 600 - 1)
    assert_scene(page, group, "Sunset")
    page.clock.run_for(1)
    assert_scene(page, group, "After dinner")

    # Headless tabs remain visible, so exercise the tab-visibility event directly.
    visibility = """hidden => {
        Object.defineProperties(document, {
            hidden: { configurable: true, value: hidden },
            visibilityState: { configurable: true, value: hidden ? "hidden" : "visible" },
        });
        document.dispatchEvent(new Event("visibilitychange"));
    }"""
    page.clock.run_for(1000)
    page.evaluate(visibility, True)
    assert play.inner_text() == "Resume"
    frozen = assert_progress(progress, 4, tolerance=1e-6)
    page.clock.run_for(2 * SCENE_DELAY)
    assert_scene(page, group, "After dinner")
    assert_progress(progress, frozen)
    page.evaluate(visibility, False)
    page.clock.run_for(2 * SCENE_DELAY)
    assert play.inner_text() == "Resume"
    assert_scene(page, group, "After dinner")
    assert_progress(progress, frozen)
    play.dispatch_event("click")
    assert_progress(progress, frozen)
    page.clock.run_for(600)
    assert_progress(progress, 4.6, tolerance=FRAME_TOLERANCE_SECONDS)
    page.clock.run_for(SCENE_DELAY - 1000 - 600 - 1)
    assert_scene(page, group, "After dinner")
    page.clock.run_for(1)
    assert_scene(page, group, "TV time")


def check_pi_landing(page, width, height):
    panel = page.get_by_role("tabpanel", name="Build with two lamps")
    assert panel.is_visible(), width
    page.wait_for_function("""() => {
        const heading = document.querySelector('#setup');
        const box = heading.getBoundingClientRect();
        return document.activeElement === heading && box.top >= 0 && box.top < window.innerHeight;
    }""", timeout=3000)
    for locator in (
        page.locator("#setup"), page.get_by_role("tablist", name="Setup path"),
        panel.locator("#setup-heading-pi"),
    ):
        box = locator.bounding_box()
        assert box is not None and 0 <= box["y"] < height, (width, locator, box)
    guide = panel.get_by_role("link", name="Start the lights-only setup", exact=True)
    assert guide.get_attribute("href").endswith("/docs/service.md#lights-only-quick-start"), width
    assert guide.bounding_box()["height"] >= 44, width


def check_parts_paths(page, width):
    parts = page.get_by_role("region", name="Check your hardware.")
    assert parts.locator(".parts-list > li").count() == 4, width
    assert parts.locator(".parts-status").bounding_box()["y"] < parts.locator(".parts-paths").bounding_box()["y"], width
    if width <= 760:
        assert parts.locator(".parts-paths").bounding_box()["y"] < parts.locator(".parts-figure").bounding_box()["y"], width
    for name, panel_id in (
        ("Try without hardware", "#setup-panel-simulator"), ("Build with two lamps", "#setup-panel-pi"),
    ):
        path = parts.get_by_role("link", name=name, exact=True)
        assert path.get_attribute("href") == panel_id, width
        assert path.bounding_box()["height"] >= 44, width
        path.click()
        assert page.evaluate("location.hash") == panel_id, width
        assert page.get_by_role("tabpanel", name=name, exact=True).is_visible(), width


def check_pi_doctor(page, width):
    panel = page.get_by_role("tabpanel", name="Build with two lamps")
    summary = panel.get_by_text("Check both plugs after installation", exact=True)
    command = panel.get_by_role("region", name="Pi diagnostic terminal command")
    assert command.count() == 0, width
    assert summary.bounding_box()["height"] >= 44, width
    summary.press("Enter")
    assert command.is_visible(), width
    panel.get_by_role("button", name="Copy commands").click()
    panel.get_by_role("button", name="Copied!").wait_for()
    assert page.evaluate("navigator.clipboard.readText()") == command.locator("code").text_content(), width
    summary.press("Enter")
    assert command.count() == 0, width


def check_report_back(page, link, return_hash, *, minimum_distance=0):
    link.scroll_into_view_if_needed()
    reading_position = page.evaluate("window.scrollY")
    link.focus()
    link.press("Enter")
    assert page.evaluate("location.hash") == "#sample-report"
    assert abs(page.evaluate("window.scrollY") - reading_position) >= minimum_distance
    assert page.locator("#sample-report").evaluate("note => document.activeElement === note")
    page.go_back()
    page.wait_for_function("position => Math.abs(window.scrollY - position) <= 2", arg=reading_position)
    assert page.evaluate("location.hash") == return_hash
    page.wait_for_function("document.activeElement.id === 'setup-simulator-report'")


def check_demo_entry_points(page, url):
    page.bring_to_front()
    page.set_viewport_size({"width": 844, "height": 390})
    page.goto(url)
    page.locator('.hero a[href="#sample-evening"]').click()
    assert page.evaluate("location.hash") == "#sample-evening"
    assert page.locator("#scene-play").inner_text() == "Pause"
    assert page.locator(".routine-scene").get_attribute("data-scene") == "kitchen"
    assert_sound_state(page, False)
    for selector in (".routine-frame", "#scene-play", "#scene-restart", "#scene-next"):
        box = page.locator(selector).bounding_box()
        assert 0 <= box["y"] < box["y"] + box["height"] <= 390, (selector, box)
    assert page.locator("#scene-play").evaluate("play => document.activeElement === play")
    page.goto(f"{url}#sample-report")
    page.reload()
    page.wait_for_function("document.activeElement.id === 'sample-report'", polling=50)
    assert_sample_report(page, True)
    page.set_viewport_size({"width": 320, "height": 568})
    page.emulate_media(reduced_motion="reduce")
    page.goto(url)
    page.reload()
    page.locator('.hero a[href="#sample-evening"]').click()
    assert_demo_ready(page)
    assert page.locator("#scene-next").evaluate("next => document.activeElement === next")
    for _ in DEMO_SCENES:
        page.locator("#scene-next").press("Enter")
    assert_sample_report(page, True)
    receipt = page.locator("#sample-report").bounding_box()
    assert 0 <= receipt["y"] < receipt["y"] + receipt["height"] <= 568, receipt


def check_scene_picker_resize(page, url):
    page.set_viewport_size({"width": 480, "height": 800})
    page.goto(url)
    picker = page.locator(".routine-picker")
    summary = picker.locator("summary")
    picker_open = "expected => document.querySelector('.routine-picker').open === expected"
    assert not picker.evaluate("picker => picker.open")
    summary.press("Enter")
    assert picker.evaluate("picker => picker.open")
    page.set_viewport_size({"width": 390, "height": 800})
    assert picker.evaluate("picker => picker.open")
    summary.press("Enter")
    assert not picker.evaluate("picker => picker.open")
    page.set_viewport_size({"width": 481, "height": 800})
    page.wait_for_function(picker_open, arg=True)
    assert summary.is_hidden()
    page.set_viewport_size({"width": 480, "height": 800})
    page.wait_for_function(picker_open, arg=False)


def check_browser(schedule):
    from playwright.sync_api import sync_playwright

    executable = shutil.which("chromium") or shutil.which("google-chrome")
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(SiteHandler, directory=SITE)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=executable)
                url = f"http://127.0.0.1:{server.server_port}/"
                for width, height in ((280, 640), (320, 640), (333, 490), (390, 844), (430, 932), (761, 900), (1440, 768)):
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, reduced_motion="reduce",
                        permissions=["clipboard-read", "clipboard-write"],
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    hero = page.locator(".hero")
                    page.evaluate("document.fonts.ready")
                    watch = hero.get_by_role("link", name="Watch an evening · 21 sec", exact=True)
                    watch_box = watch.bounding_box()
                    assert watch.get_attribute("href") == "#sample-evening"
                    assert watch_box["height"] >= 44 and watch_box["y"] + watch_box["height"] <= height, width
                    build = hero.get_by_role("link", name="Build it on a Pi", exact=True)
                    assert build.get_attribute("href") == "#parts" and build.bounding_box()["height"] >= 44, width
                    if width <= 760:
                        image = hero.locator(".hero-figure").bounding_box()
                        assert watch_box["y"] < image["y"] < height, width
                        assert build.bounding_box()["y"] >= image["y"] + image["height"], width
                    check_scene_selection(page, width, height, schedule)
                    check_parts_paths(page, width)
                    pi = page.get_by_role("tab", name="Build with two lamps")
                    check_pi_landing(page, width, height)
                    check_pi_doctor(page, width)
                    page.get_by_role("link", name="What you need", exact=True).click()
                    page.get_by_role("link", name="Setup", exact=True).click()
                    assert pi.get_attribute("aria-selected") == "true"
                    pi.focus()
                    page.keyboard.press("ArrowLeft")
                    assert page.evaluate("location.hash") == "#setup-panel-simulator"
                    simulator = page.get_by_role("tabpanel", name="Try without hardware")
                    simulator.get_by_role("button", name="Copy commands").click()
                    simulator.get_by_role("button", name="Copied!").wait_for()
                    assert page.evaluate("navigator.clipboard.readText()") == simulator.locator("#setup-code-simulator").text_content()
                    simulator.get_by_text("See example JSON fields", exact=True).click()
                    assert simulator.locator("#sample-report-json").is_visible()
                    note_link = simulator.get_by_role("link", name="example morning note")
                    check_report_back(page, note_link, "#setup-panel-simulator", minimum_distance=100)
                    assert simulator.is_visible()
                    assert page.evaluate("document.documentElement.scrollWidth === innerWidth"), width
                    page.goto(f"{url}#setup-panel-pi")
                    page.reload()
                    check_pi_landing(page, width, height)
                    page.goto(f"{url}#setup-panel-simulator")
                    page.reload()
                    assert simulator.is_visible()
                    assert not errors, errors
                    context.close()
                with browser.new_context() as context:
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    check_demo_playback(page, url)
                    assert not errors, errors
                    check_demo_selected_start(context.new_page(), url)
                    check_demo_auto_pause(context.new_page(), url)
                    check_scene_picker_resize(context.new_page(), url)
                    check_demo_entry_points(context.new_page(), url)
                page = browser.new_context(java_script_enabled=False, reduced_motion="reduce").new_page()
                page.bring_to_front()
                page.goto(f"{url}#setup-panel-pi")
                for selector in ("#setup-panel-simulator", "#setup-panel-pi", "#sample-report"):
                    assert page.locator(selector).is_visible(), selector
                assert page.locator("#sample-report-text").inner_text().strip(), "Static morning note is empty"
                assert page.locator(".routine-interactions").is_hidden()
                assert page.locator(".routine-scene").get_attribute("data-scene") == "kitchen"
                assert_scheduled_state(page, "Sunset", schedule)
                assert_window_lamps(page, 1, 0)
                transcript = page.get_by_text("Read the sample evening", exact=True)
                transcript.press("Enter")
                assert page.locator(".routine-transcript ol").is_visible()
                assert_sample_report(page, True)
        finally:
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="also exercise the pages in Chromium using Playwright")
    args = parser.parse_args()
    page = PageSource()
    page.feed((SITE / "index.html").read_text(encoding="utf-8"))
    check_source(page)
    schedule = check_simulator(page)
    print("Website links, assets, shell snippets, and pinned simulator output passed.")
    if args.browser:
        check_browser(schedule)
        print("Browser checks across seven viewport sizes and without JavaScript passed.")


if __name__ == "__main__":
    sys.exit(main())
