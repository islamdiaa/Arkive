#!/usr/bin/env python3
"""Release helpers for CI: CHANGELOG.md sections, version checks, image tags.

Standard library only, so the workflow can run it without installing anything.

  release.py check [--tag vX.Y.Z]
      Validate CHANGELOG.md (Keep a Changelog layout, versions newest first,
      a link for every section) and check that the version files agree with
      each other and have a CHANGELOG section. With --tag, also require a
      non-empty section for that tag and, for a stable tag, version files that
      match it. CI runs this before anything is built.

  release.py notes vX.Y.Z
      Print the release notes for a tag: its CHANGELOG section plus the
      "Full changelog" link. A pre-release tag without its own section uses
      the [Unreleased] section.

  release.py tag-info vX.Y.Z  < tags
      Read the repository's tags on stdin (``git ls-remote --tags`` output or
      one tag per line) and print key=value lines for $GITHUB_OUTPUT:
      version, prerelease, latest (newest stable release overall) and
      newest_in_major (newest stable release with this major version).
      Fails, printing nothing, when the list doesn't include vX.Y.Z itself
      (an empty or truncated listing).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEMVER = r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<pre>[0-9A-Za-z.-]+))?"
TAG_RE = re.compile(rf"v{SEMVER}")
VERSION_RE = re.compile(SEMVER)
HEADING_RE = re.compile(r"## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?")
LINK_RE = re.compile(r"\[(?P<name>[^\]]+)\]:\s+(?P<url>\S+)\s*")
UNRELEASED = "Unreleased"


class ReleaseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def parse_tag(tag: str) -> tuple[str, bool]:
    """Return (version, is_prerelease) for a tag like ``v1.2.3`` or ``v1.2.3-rc.1``."""
    m = TAG_RE.fullmatch(tag)
    if not m:
        raise ReleaseError(
            f"{tag!r} is not a version tag (expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-PRERELEASE)"
        )
    return tag[1:], m["pre"] is not None


def version_key(version: str) -> tuple:
    """Sort key following semver precedence (a pre-release sorts before its release)."""
    m = VERSION_RE.fullmatch(version)
    if not m:
        raise ReleaseError(f"{version!r} is not a semantic version")
    core = (int(m["major"]), int(m["minor"]), int(m["patch"]))
    if m["pre"] is None:
        return (core, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m["pre"].split("."))
    return (core, 0, ids)


def tag_info(tag: str, existing_tags: list[str]) -> dict[str, str]:
    """Decide which moving tags (``latest``, the major version) this tag may take.

    Only a stable release that is at least as new as every other stable release
    moves them, so a fix tagged on an older line (v0.4.1 after v0.5.0) or a
    pre-release never pulls users of ``latest`` backwards or onto an RC.

    ``existing_tags`` is the repository's full tag list, which includes the
    tag being released (it was pushed before CI ran). A list without it means
    the listing failed or came back empty, and without the other tags this
    tag would look like the newest release, so that is an error, not a guess.
    """
    version, prerelease = parse_tag(tag)
    if tag not in existing_tags:
        raise ReleaseError(
            f"the tag list does not include {tag} itself, so it is incomplete "
            f"({len(existing_tags)} tag(s) read; did 'git ls-remote --tags' fail?). "
            "Refusing to decide whether this tag may move 'latest'."
        )
    stable = [version]
    for name in existing_tags:
        m = TAG_RE.fullmatch(name)
        if m and m["pre"] is None:
            stable.append(name[1:])
    key = version_key(version)
    major = key[0][0]
    newest = max(stable, key=version_key)
    newest_in_major = max((v for v in stable if version_key(v)[0][0] == major), key=version_key)
    return {
        "version": version,
        "prerelease": _bool(prerelease),
        "latest": _bool(not prerelease and version_key(newest) == key),
        "newest_in_major": _bool(not prerelease and version_key(newest_in_major) == key),
    }


def read_tag_list(text: str) -> list[str]:
    """Tag names from ``git ls-remote --tags`` output or a plain list."""
    tags = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[-1].removeprefix("refs/tags/").removesuffix("^{}")
        tags.append(name)
    return tags


def _bool(value: bool) -> str:
    return "true" if value else "false"


# ---------------------------------------------------------------------------
# Version files
# ---------------------------------------------------------------------------


def _regex_version(pattern: str):
    regex = re.compile(pattern, re.MULTILINE)

    def read(text: str) -> str | None:
        m = regex.search(text)
        return m.group(1) if m else None

    return read


def _json_version(*keys: str):
    def read(text: str) -> str | None:
        node = json.loads(text)
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node if isinstance(node, str) else None

    return read


# Every place the release version is written down. The image build passes the
# tag's version in at build time, but these are what a dev build, the API
# fallback and the package metadata report, so they move together.
VERSION_FILES = [
    ("backend/pyproject.toml", _regex_version(r'^version\s*=\s*"([^"]+)"')),
    ("backend/app/__init__.py", _regex_version(r'^__version__\s*=.*"([^"]+)"\s*$')),
    ("frontend/package.json", _json_version("version")),
    ("frontend/package-lock.json", _json_version("version")),
    ("frontend/package-lock.json", _json_version("packages", "", "version")),
    ("Dockerfile", _regex_version(r"^ARG VERSION=(\S+)\s*$")),
]


def read_versions(root: Path) -> list[tuple[str, str]]:
    """(file, version) for every version file; raises if one can't be read."""
    found = []
    for rel, reader in VERSION_FILES:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseError(f"{rel}: cannot read ({exc.strerror})") from exc
        try:
            version = reader(text)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{rel}: invalid JSON ({exc})") from exc
        if not version:
            raise ReleaseError(f"{rel}: no version found")
        found.append((rel, version))
    return found


# ---------------------------------------------------------------------------
# CHANGELOG.md
# ---------------------------------------------------------------------------


@dataclass
class Section:
    name: str
    date: str | None
    line: int
    body: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.body).strip("\n")


@dataclass
class Changelog:
    sections: list[Section]
    links: dict[str, str]

    def get(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)


def parse_changelog(text: str) -> Changelog:
    sections: list[Section] = []
    links: dict[str, str] = {}
    current: Section | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            m = HEADING_RE.fullmatch(line.rstrip())
            if not m:
                raise ReleaseError(
                    f"CHANGELOG.md:{number}: heading {line.strip()!r} should be "
                    "'## [Unreleased]' or '## [X.Y.Z] - YYYY-MM-DD'"
                )
            current = Section(m["name"], m["date"], number)
            sections.append(current)
            continue
        link = LINK_RE.fullmatch(line)
        if link:
            links[link["name"]] = link["url"]
            continue
        if current is not None:
            current.body.append(line)
    return Changelog(sections, links)


def validate_changelog(log: Changelog) -> list[str]:
    errors: list[str] = []
    if not log.sections or log.sections[0].name != UNRELEASED:
        errors.append("CHANGELOG.md: the first section must be '## [Unreleased]'")
    seen: set[str] = set()
    previous: Section | None = None
    for section in log.sections:
        where = f"CHANGELOG.md:{section.line}"
        if section.name in seen:
            errors.append(f"{where}: duplicate section [{section.name}]")
        seen.add(section.name)
        if section.name not in log.links:
            errors.append(f"{where}: no link definition '[{section.name}]: <url>' at the end of the file")
        if section.name == UNRELEASED:
            if section is not log.sections[0]:
                errors.append(f"{where}: [Unreleased] must be the first section")
            if section.date:
                errors.append(f"{where}: [Unreleased] takes no date")
            continue
        if not VERSION_RE.fullmatch(section.name):
            errors.append(f"{where}: [{section.name}] is not a semantic version")
            continue
        if not section.date:
            errors.append(f"{where}: [{section.name}] needs a release date (' - YYYY-MM-DD')")
        else:
            try:
                datetime.date.fromisoformat(section.date)
            except ValueError:
                errors.append(f"{where}: {section.date} is not a valid date")
        if not section.text.strip():
            errors.append(f"{where}: [{section.name}] is empty")
        if previous is not None and version_key(section.name) >= version_key(previous.name):
            errors.append(f"{where}: [{section.name}] must come after [{previous.name}] (newest release first)")
        previous = section
    return errors


def load_changelog(root: Path) -> Changelog:
    path = root / "CHANGELOG.md"
    try:
        return parse_changelog(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"CHANGELOG.md: cannot read ({exc.strerror})") from exc


def release_notes(log: Changelog, tag: str) -> str:
    version, prerelease = parse_tag(tag)
    section = log.get(version)
    if section is None and prerelease:
        section = log.get(UNRELEASED)
    if section is None:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}; add '## [{version}] - YYYY-MM-DD'")
    body = section.text
    if not body.strip():
        raise ReleaseError(f"CHANGELOG.md: the section used for {tag} ([{section.name}]) is empty")
    link = log.links.get(section.name)
    if link and section.name != UNRELEASED:
        body += f"\n\n**Full changelog:** {link}"
    return body + "\n"


def check(root: Path, tag: str | None = None) -> list[str]:
    """All problems found; an empty list means the release metadata is consistent."""
    try:
        log = load_changelog(root)
    except ReleaseError as exc:
        return [str(exc)]
    errors = validate_changelog(log)

    try:
        versions = read_versions(root)
    except ReleaseError as exc:
        return [*errors, str(exc)]
    distinct = sorted({v for _, v in versions})
    if len(distinct) > 1:
        listing = ", ".join(f"{rel}={v}" for rel, v in versions)
        errors.append(f"version files disagree: {listing}")
    else:
        current = distinct[0]
        if log.get(current) is None:
            errors.append(f"version files say {current} but CHANGELOG.md has no [{current}] section")

    if tag:
        try:
            version, prerelease = parse_tag(tag)
        except ReleaseError as exc:
            return [*errors, str(exc)]
        try:
            release_notes(log, tag)
        except ReleaseError as exc:
            errors.append(str(exc))
        if not prerelease:
            wrong = [f"{rel}={v}" for rel, v in versions if v != version]
            if wrong:
                errors.append(f"tag {tag} does not match the version files: {', '.join(wrong)}")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _report(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}", file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="validate CHANGELOG.md and version files")
    p_check.add_argument("--tag", default="", help="also check release readiness for this tag (empty: skip)")
    p_notes = sub.add_parser("notes", help="print the release notes for a tag")
    p_notes.add_argument("tag")
    p_info = sub.add_parser("tag-info", help="print $GITHUB_OUTPUT lines for a tag; existing tags on stdin")
    p_info.add_argument("tag")
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            errors = check(args.root, args.tag or None)
            for error in errors:
                _report(error)
            if errors:
                return 1
            print("CHANGELOG.md and version files are consistent" + (f" for {args.tag}" if args.tag else ""))
        elif args.command == "notes":
            sys.stdout.write(release_notes(load_changelog(args.root), args.tag))
        elif args.command == "tag-info":
            for key, value in tag_info(args.tag, read_tag_list(sys.stdin.read())).items():
                print(f"{key}={value}")
    except ReleaseError as exc:
        _report(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
