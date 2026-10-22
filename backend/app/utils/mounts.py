"""Detect whether a path inside the container is backed by persistent storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")


@dataclass
class MountEntry:
    root: str  # path within the source filesystem
    mount_point: str
    fstype: str


def _unescape(value: str) -> str:
    # mountinfo octal-escapes spaces, tabs, newlines and backslashes.
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def read_mounts(mountinfo: Path = MOUNTINFO) -> list[MountEntry]:
    entries: list[MountEntry] = []
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return entries
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5:
            continue
        fstype = right.split()[0] if right.split() else ""
        entries.append(MountEntry(root=_unescape(fields[3]), mount_point=_unescape(fields[4]), fstype=fstype))
    return entries


def mount_for_path(path: str, mounts: list[MountEntry]) -> MountEntry | None:
    """Return the mount whose mount point is the longest prefix of ``path``."""
    normalized = os.path.normpath(path)
    best: MountEntry | None = None
    for entry in mounts:
        mp = entry.mount_point
        if normalized == mp or normalized.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) > len(best.mount_point):
                best = entry
    return best


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def ephemeral_path_reason(path: str, mountinfo: Path = MOUNTINFO, in_container: bool | None = None) -> str | None:
    """Explain why ``path`` would not survive a container re-create, or None.

    Data written to the container's root filesystem or to an anonymous Docker
    volume is lost when the container is removed (e.g. on image update), which
    for a local backup target means silently losing every snapshot.
    """
    if in_container is None:
        in_container = running_in_container()
    if not in_container:
        return None
    entry = mount_for_path(path, read_mounts(mountinfo))
    if entry is None:
        return None
    if entry.mount_point == "/":
        return "it is inside the container's own filesystem"
    root = entry.root.rstrip("/")
    if "/volumes/" in root and root.endswith("/_data"):
        name = root.split("/volumes/", 1)[1].split("/", 1)[0]
        if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
            return f"it is on an anonymous Docker volume mounted at {entry.mount_point}"
    return None
