#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown", "packaging", "pyyaml"]
# ///
"""Move the package to a new Anki release. See updating-anki.md.

Edits the manifest, the vendored sources, requirements.in and the metainfo
in place. Exits with status 2 if something needs a manual look; the report
(stdout, and --report if given) says what.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import markdown
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "net.ankiweb.Anki.yaml"
METAINFO = ROOT / "net.ankiweb.Anki.metainfo.xml"
ANKI = ROOT / "anki"
BUILDER_TOOLS = ROOT / "flatpak-builder-tools"

ANKI_URL = "https://github.com/ankitects/anki"
BUILDER_TOOLS_URL = "https://github.com/flatpak/flatpak-builder-tools"
YARN_URL = (
    "https://raw.githubusercontent.com/yarnpkg/berry/refs/tags/"
    "%40yarnpkg/cli/{version}/packages/yarnpkg-cli/bin/yarn.js"
)

# PyQt comes from the BaseApp, not from pip.
PYQT_PACKAGES = {"pyqt6", "pyqt6-qt6", "pyqt6-webengine", "pyqt6-webengine-qt6"}
LINUX_MARKER_ENV = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"}
APPSTREAM_TAGS = ("p", "ul", "ol", "li", "em", "code")


def run(*cmd: str | Path, **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT, **kwargs)


def fetch(url: str) -> bytes:
    headers = {"User-Agent": "net.ankiweb.Anki-updater"}
    if url.startswith("https://api.github.com/") and (token := os.environ.get("GITHUB_TOKEN")):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
        return resp.read()


def checkout_anki(tag: str) -> str:
    if not ANKI.exists():
        run("git", "clone", "--filter=blob:none", ANKI_URL, ANKI)
    run("git", "-C", ANKI, "fetch", "--tags", "--force", "origin")
    run("git", "-C", ANKI, "checkout", "--quiet", "--detach", f"refs/tags/{tag}")
    return run("git", "-C", ANKI, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()


def ensure_builder_tools() -> None:
    if not BUILDER_TOOLS.exists():
        run("git", "clone", "--depth=1", BUILDER_TOOLS_URL, BUILDER_TOOLS)


def replace_once(pattern: str, repl, text: str, what: str) -> str:
    new, count = re.subn(pattern, repl, text)
    if count != 1:
        sys.exit(f"expected one match for {what} in the manifest, found {count}")
    return new


def update_anki_source(manifest: str, tag: str, commit: str) -> str:
    # Quote the tag: bare 26.10 is a YAML float.
    return replace_once(
        r"(url: https://github\.com/ankitects/anki\.git\n(\s+)tag: ).*\n\s+commit: .*",
        lambda m: f"{m[1]}'{tag}'\n{m[2]}commit: {commit}",
        manifest,
        "the anki git source",
    )


def update_yarn(manifest: str, notes: list[str]) -> str:
    package_manager = json.loads((ANKI / "package.json").read_text(encoding="utf-8"))["packageManager"]
    m = re.fullmatch(r"yarn@([^+]+)(\+.*)?", package_manager)
    if not m:
        sys.exit(f"unexpected packageManager in package.json: {package_manager}")
    version = m[1]
    pattern = (
        r"(url: )" + re.escape(YARN_URL).replace(r"\{version\}", r"([^/]+)") + r"(\n\s+sha256: )([0-9a-f]{64})"
    )
    current = re.search(pattern, manifest)
    if not current:
        sys.exit("could not find the yarn.js source in the manifest")
    if current[2] == version:
        return manifest
    url = YARN_URL.format(version=version)
    sha256 = hashlib.sha256(fetch(url)).hexdigest()
    notes.append(f"Bumped yarn from {current[2]} to {version}.")
    return manifest[: current.start()] + f"url: {url}{current[3]}{sha256}" + manifest[current.end() :]


def write_requirements_in() -> None:
    lines: list[str] = []
    seen: set[str] = set()
    for label, path in (("anki (pylib)", "pylib"), ("aqt", "qt")):
        project = tomllib.loads((ANKI / path / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        if lines:
            lines.append("")
        lines.append(f"# {label}")
        for dep in project["dependencies"]:
            req = Requirement(dep)
            name = canonicalize_name(req.name)
            if name in PYQT_PACKAGES or name in seen:
                continue
            if req.marker and not req.marker.evaluate(LINUX_MARKER_ENV):
                continue
            seen.add(name)
            lines.append(dep)
    (ROOT / "requirements.in").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def regenerate_sources(problems: list[str]) -> None:
    run(sys.executable, "scripts/generate-cargo-sources.py", "anki/Cargo.lock", "cargo-sources.json")

    run(
        "uvx", "--from", "./flatpak-builder-tools/node", "flatpak-node-generator",
        "yarn", "anki/yarn.lock", "-o", "yarn-sources.json",
    )
    # The manifest imports this plugin explicitly.
    if "flatpak-yarn.js" not in (ROOT / "yarn-sources.json").read_text(encoding="utf-8"):
        problems.append("`yarn-sources.json` has no `flatpak-yarn.js` entry. The build will fail.")

    run("bash", "scripts/generate-py-sources.sh")


def check_patches(problems: list[str]) -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    anki_module = next(m for m in manifest["modules"] if isinstance(m, dict) and m.get("name") == "anki")
    for source in anki_module["sources"]:
        if not isinstance(source, dict) or source.get("type") != "patch":
            continue
        for patch in source.get("paths", [source.get("path")]):
            result = subprocess.run(
                ["git", "-C", str(ANKI), "apply", "--check", str(ROOT / patch)],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                problems.append(f"`{patch}` does not apply:\n\n```\n{result.stderr.strip()}\n```")


def md_to_appstream(md_text: str) -> str:
    md_text = md_text.replace("\r\n", "\n")
    md_text = re.sub(r"^#+ What's Changed\s*$", "", md_text, flags=re.MULTILINE)
    html = markdown.markdown(md_text)
    html = re.sub(r"<(/?)(?:strong|b)>", r"<\1em>", html)
    html = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"<p>\1</p>", html, flags=re.DOTALL)
    html = re.sub(r"<br\s*/?>", " ", html)
    # <p> is not allowed inside <li> (loose Markdown lists produce it).
    html = re.sub(
        r"<li>(.*?)</li>",
        lambda m: "<li>" + re.sub(r"</?p>", "", m[1]).strip() + "</li>",
        html,
        flags=re.DOTALL,
    )
    allowed = "|".join(APPSTREAM_TAGS)
    html = re.sub(rf"</?(?!(?:{allowed})>)[a-zA-Z][^>]*>", "", html)
    return html.strip()


def update_metainfo(tag: str, release: dict) -> None:
    date = release["published_at"][:10]
    body = "\n".join(
        f"        {line}" if line else "" for line in md_to_appstream(release.get("body") or "").splitlines()
    )
    entry = (
        f'  <releases>\n    <release version="{tag}" date="{date}">\n'
        f"      <description>\n{body}\n      </description>\n    </release>\n  </releases>"
    )
    text = METAINFO.read_text(encoding="utf-8")
    new, count = re.subn(r"  <releases>.*?</releases>", lambda _: entry, text, flags=re.DOTALL)
    if count != 1:
        sys.exit("could not find <releases> in the metainfo")
    METAINFO.write_text(new, encoding="utf-8", newline="\n")


def validate_metainfo(problems: list[str], notes: list[str]) -> None:
    if not shutil.which("appstreamcli"):
        notes.append("Skipped `appstreamcli validate` (not installed).")
        return
    result = subprocess.run(
        ["appstreamcli", "validate", "--no-net", "--explain", str(METAINFO)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        problems.append(f"`appstreamcli validate` failed:\n\n```\n{result.stdout.strip()}\n```")


def write_report(tag: str, notes: list[str], problems: list[str], path: str | None) -> None:
    parts = []
    if problems:
        parts.append("## Needs attention\n\n" + "\n\n".join(f"- {p}" for p in problems))
    if notes:
        parts.append("## Notes\n\n" + "\n".join(f"- {n}" for n in notes))
    parts.append(
        "## Before merging\n\n"
        "- [ ] Review the `requirements.in` diff against the upstream pyproject files.\n"
        "- [ ] Check the Flathub test build, then install and try it (step 8 in `updating-anki.md`)."
    )
    report = f"Updates Anki to [{tag}]({ANKI_URL}/releases/tag/{tag}).\n\n" + "\n\n".join(parts) + "\n"
    print(report)
    if path:
        Path(path).write_text(report, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="Anki release tag, e.g. 26.10")
    parser.add_argument("--report", help="also write the Markdown report to this file")
    args = parser.parse_args()

    notes: list[str] = []
    problems: list[str] = []

    release = json.loads(fetch(f"https://api.github.com/repos/ankitects/anki/releases/tags/{args.tag}"))
    commit = checkout_anki(args.tag)
    ensure_builder_tools()

    manifest = MANIFEST.read_text(encoding="utf-8")
    manifest = update_anki_source(manifest, args.tag, commit)
    manifest = update_yarn(manifest, notes)
    MANIFEST.write_text(manifest, encoding="utf-8", newline="\n")

    write_requirements_in()
    regenerate_sources(problems)
    check_patches(problems)
    update_metainfo(args.tag, release)
    validate_metainfo(problems, notes)

    write_report(args.tag, notes, problems, args.report)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
