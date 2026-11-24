"""Check the project website and the copyable simulator example."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
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
    for url in [*page.links, *assets]:
        parts = urlsplit(url)
        if not parts.netloc:
            assert (SITE / parts.path).is_file() if parts.path else parts.fragment in page.ids, url
        elif url.startswith(f"{REPOSITORY}/blob/"):
            ref, path = parts.path.split("/blob/", 1)[1].split("/", 1)
            if ref == "main":
                assert path in {"LICENSE", "CONTRIBUTING.md"} and (ROOT / path).is_file(), url
                continue
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


class SiteHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def check_browser():
    from playwright.sync_api import sync_playwright

    executable = shutil.which("chromium") or shutil.which("google-chrome")
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(SiteHandler, directory=SITE)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=executable)
                url = f"http://127.0.0.1:{server.server_port}/"
                for width, height in ((280, 640), (320, 640), (390, 844), (430, 932), (761, 900), (1440, 940)):
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, reduced_motion="reduce",
                        permissions=["clipboard-read", "clipboard-write"],
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.get_by_role("link", name="Try the browser demo").click()
                    scenes = page.get_by_role("group", name="Sample evening scenes")
                    assert scenes.bounding_box()["y"] < height, width
                    scenes.get_by_role("button", name="After dinner").click()
                    assert page.locator("#scene-time").inner_text() == "8:10 PM"
                    assert page.locator("#sample-report").is_visible()
                    page.get_by_role("link", name="Build on a Raspberry Pi").click()
                    pi = page.get_by_role("tab", name="Install on a Pi")
                    panel = page.get_by_role("tabpanel", name="Install on a Pi")
                    panel.get_by_role("link", name="Install the pinned developer preview").wait_for()
                    page.get_by_role("link", name="What you need").click()
                    if width <= 760:
                        assert page.locator(".parts-list").bounding_box()["y"] < page.locator(".parts-figure").bounding_box()["y"]
                    page.get_by_role("link", name="Setup", exact=True).click()
                    assert pi.get_attribute("aria-selected") == "true"
                    pi.focus()
                    page.keyboard.press("ArrowLeft")
                    assert page.evaluate("location.hash") == "#setup-panel-simulator"
                    simulator = page.get_by_role("tabpanel", name="Run the Linux simulator")
                    simulator.get_by_role("button", name="Copy commands").click()
                    simulator.get_by_role("button", name="Copied!").wait_for()
                    assert page.evaluate("navigator.clipboard.readText()") == simulator.locator("pre code").text_content()
                    assert page.evaluate("document.documentElement.scrollWidth === innerWidth"), width
                    page.goto(f"{url}#setup-panel-pi")
                    assert panel.is_visible()
                    assert not errors, errors
                    context.close()
                page = browser.new_context(java_script_enabled=False).new_page()
                page.goto(f"{url}#setup-panel-pi")
                for selector in ("#setup-panel-simulator", "#setup-panel-pi", "#sample-report"):
                    assert page.locator(selector).is_visible(), selector
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
    check_simulator(page)
    print("Website links, assets, shell snippets, and pinned simulator output passed.")
    if args.browser:
        check_browser()
        print("Browser checks at six widths and without JavaScript passed.")


if __name__ == "__main__":
    sys.exit(main())
