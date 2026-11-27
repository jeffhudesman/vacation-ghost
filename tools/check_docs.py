"""Check the Markdown guides and keep their published simulator example in sync."""

import json
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from check_site import PageSource, REPOSITORY, ROOT, SITE


FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\((?:<([^>\n]+)>|([^\s)]+))\)")


def markdown_anchors(source: str) -> set[str]:
    anchors = set()
    for heading in re.findall(r"^#{1,6} +(.+)$", source, re.MULTILINE):
        base = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        anchor = base
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{base}-{suffix}"
        anchors.add(anchor)
    return anchors


def check_link(document: Path, target: str, page: PageSource) -> bool:
    parts = urlsplit(target)
    fragment = unquote(parts.fragment)
    project_docs = f"{REPOSITORY}/blob/main/"
    if target.startswith(project_docs):
        path = ROOT / unquote(target.split(project_docs, 1)[1].split("#", 1)[0])
    elif parts.netloc == "vacationghost.com":
        path = SITE / (unquote(parts.path).lstrip("/") or "index.html")
    elif parts.scheme or parts.netloc:
        return False
    else:
        path = document.parent / unquote(parts.path) if parts.path else document

    if not path.resolve().is_relative_to(ROOT):
        raise ValueError("link leaves the repository")
    if not path.exists():
        raise ValueError(f"missing file {path}")
    if not fragment:
        return True
    if path.suffix == ".md":
        source = FENCE.sub("", path.read_text(encoding="utf-8"))
        if fragment not in markdown_anchors(source):
            raise ValueError(f"missing Markdown heading #{fragment}")
    elif path.resolve() == SITE / "index.html" and fragment not in page.ids:
        raise ValueError(f"missing website anchor #{fragment}")
    return True


def main() -> None:
    page = PageSource()
    page.feed((SITE / "index.html").read_text(encoding="utf-8"))
    files = subprocess.check_output(
        ["git", "-C", ROOT, "ls-files", "-z", "--", "*.md"], text=True,
    ).rstrip("\0").split("\0")
    snippets = links = 0
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in files:
        document = ROOT / name
        source = document.read_text(encoding="utf-8")
        for fence in FENCE.finditer(source):
            language, code = fence.group(1).strip(), fence.group(2)
            location = f"{name}:{source.count(chr(10), 0, fence.start()) + 1}"
            if language in {"sh", "bash"}:
                result = subprocess.run([language, "-n"], input=code, text=True, capture_output=True)
                if result.returncode:
                    raise SystemExit(f"{location}: {result.stderr.strip()}")
                snippets += 1
            elif language == "toml":
                try:
                    tomllib.loads(code)
                except tomllib.TOMLDecodeError as error:
                    raise SystemExit(f"{location}: {error}") from error
                snippets += 1
        # Preserve line numbers and ignore examples of Markdown or shell syntax in fences.
        prose = FENCE.sub(lambda match: "\n" * match.group().count("\n"), source)
        for number, line in enumerate(prose.splitlines(), 1):
            for link in LINK.finditer(line):
                target = link.group(1) or link.group(2)
                try:
                    if check_link(document, target, page):
                        links += 1
                except ValueError as error:
                    raise SystemExit(f"{name}:{number}: {target}: {error}") from error

    commands = page.text["setup-code-simulator"].strip()
    if commands not in [match.group(2).strip() for match in FENCE.finditer(readme) if match.group(1) == "sh"]:
        raise SystemExit("The README simulator commands and website simulator commands differ.")
    expected_message = json.loads(page.text["sample-report-json"])["message"]
    if f"> {expected_message}" not in readme.splitlines():
        raise SystemExit("The README sample message differs from the website's checked simulator report.")
    release_links = [link for link in page.links if link.startswith(f"{REPOSITORY}/releases/tag/")]
    release_url, = release_links
    tag = release_url.rsplit("/", 1)[-1]
    service_guide = (ROOT / "docs/service.md").read_text(encoding="utf-8")
    if release_url not in readme or release_url not in service_guide or f"release_tag={tag}" not in service_guide.splitlines():
        raise SystemExit("The README, website and Pi install guide must identify the same public preview.")
    print(f"Checked {len(files)} documents, {links} project links, {snippets} shell/TOML examples and preview consistency.")


if __name__ == "__main__":
    main()
