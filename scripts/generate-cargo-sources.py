#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlparse

CRATE_URL = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
GIT_SRC_RE = re.compile(r"git\+(?P<url>[^?#]+)\?rev=(?P<rev>[0-9a-f]+)")


def parse_git_source(source: str) -> tuple[str, str]:
    m = GIT_SRC_RE.match(source)
    if not m:
        raise ValueError(f"unsupported git source: {source}")
    url = m["url"].removesuffix(".git")
    return url, m["rev"]


def repo_dirname(url: str, rev: str) -> str:
    name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return f"flatpak-cargo/git/{name}-{rev[:7]}"


def discover_git_packages(url: str, rev: str, wanted: set[str]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--quiet", url, tmp], check=True)
        subprocess.run(["git", "-C", tmp, "checkout", "--quiet", rev], check=True)
        for toml_path in Path(tmp).rglob("Cargo.toml"):
            try:
                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            name = data.get("package", {}).get("name")
            if name in wanted:
                subdir = toml_path.parent.relative_to(tmp).as_posix() or "."
                found[name] = (subdir, toml_path.read_text(encoding="utf-8"))
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(f"packages not found in {url}@{rev}: {sorted(missing)}")
    return found


def crate_entries(pkg: dict) -> list[dict]:
    name, version, sha = pkg["name"], pkg["version"], pkg["checksum"]
    dest = f"cargo/vendor/{name}-{version}"
    return [
        {
            "type": "archive",
            "archive-type": "tar-gzip",
            "url": CRATE_URL.format(name=name, version=version),
            "sha256": sha,
            "dest": dest,
        },
        {
            "type": "inline",
            "contents": json.dumps({"package": sha, "files": {}}),
            "dest": dest,
            "dest-filename": ".cargo-checksum.json",
        },
    ]


def git_pkg_entries(pkg: dict, git_url: str, rev: str, subdir: str, cargo_toml: str) -> list[dict]:
    vendor = f"cargo/vendor/{pkg['name']}"
    src = f"{repo_dirname(git_url, rev)}/{subdir}"
    return [
        {
            "type": "shell",
            "commands": [f'cp -r --reflink=auto "{src}" "{vendor}"'],
        },
        {
            "type": "inline",
            "contents": cargo_toml,
            "dest": vendor,
            "dest-filename": "Cargo.toml",
        },
        {
            "type": "inline",
            "contents": json.dumps({"package": None, "files": {}}),
            "dest": vendor,
            "dest-filename": ".cargo-checksum.json",
        },
    ]


def build_cargo_config(git_sources: dict[str, str]) -> str:
    lines = [
        "[source.vendored-sources]",
        'directory = "cargo/vendor"',
        "",
        "[source.crates-io]",
        'replace-with = "vendored-sources"',
        "",
    ]
    for url in sorted(git_sources):
        lines += [
            f'[source."{url}"]',
            f'git = "{url}"',
            'replace-with = "vendored-sources"',
            f'rev = "{git_sources[url]}"',
            "",
        ]
    return "\n".join(lines)


def main(lock_path: str, out_path: str) -> None:
    with open(lock_path, "rb") as f:
        lock = tomllib.load(f)

    crate_pkgs: list[dict] = []
    git_pkgs: list[tuple[dict, str]] = []  # (pkg, git_url)
    git_sources: dict[str, str] = {}       # git_url -> rev

    for pkg in lock.get("package", []):
        src = pkg.get("source")
        if not src:
            continue
        if src.startswith("registry+"):
            crate_pkgs.append(pkg)
        elif src.startswith("git+"):
            url, rev = parse_git_source(src)
            existing = git_sources.setdefault(url, rev)
            if existing != rev:
                raise RuntimeError(f"conflicting revs for {url}: {existing} vs {rev}")
            git_pkgs.append((pkg, url))
        else:
            raise RuntimeError(f"unknown source: {src}")

    git_meta: dict[tuple[str, str], tuple[str, str]] = {}  # (url, name) -> (subdir, toml)
    for url, rev in git_sources.items():
        wanted = {p["name"] for p, u in git_pkgs if u == url}
        for name, info in discover_git_packages(url, rev, wanted).items():
            git_meta[(url, name)] = info

    sources: list[dict] = []

    for url in sorted(git_sources):
        sources.append({
            "type": "git",
            "url": url,
            "commit": git_sources[url],
            "dest": repo_dirname(url, git_sources[url]),
        })

    everything = (
        [(p["name"], "crate", p) for p in crate_pkgs]
        + [(p["name"], "git", (p, u)) for p, u in git_pkgs]
    )
    for _name, kind, payload in sorted(everything, key=lambda x: x[0]):
        if kind == "crate":
            sources += crate_entries(payload)
        else:
            pkg, url = payload
            subdir, cargo_toml = git_meta[(url, pkg["name"])]
            sources += git_pkg_entries(pkg, url, git_sources[url], subdir, cargo_toml)

    sources.append({
        "type": "inline",
        "contents": build_cargo_config(git_sources),
        "dest": "cargo",
        "dest-filename": "config",
    })

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sources, f, indent=4)
    print(f"Wrote {len(sources)} sources to {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("args: <Cargo.lock> <output.json>")
    main(sys.argv[1], sys.argv[2])