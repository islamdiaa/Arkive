"""Detect whether a path inside the container is backed by persistent storage."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MOUNTINFO = Path("/proc/self/mountinfo")


@dataclass
class MountEntry:
    root: str  # path within the source filesystem
    mount_point: str
    fstype: str
    options: list[str] = field(default_factory=list)  # per-mount options (rw/ro, ...)

    @property
    def read_only(self) -> bool:
        return "ro" in self.options


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
        options = fields[5].split(",") if len(fields) > 5 else []
        entries.append(MountEntry(root=_unescape(fields[3]), mount_point=_unescape(fields[4]),
                                  fstype=fstype, options=options))
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


def read_only_mount_for(path: str, mountinfo: Path = MOUNTINFO) -> str | None:
    """Mount point of the read-only mount containing ``path``, or None if writable.

    For a path that doesn't exist yet, the nearest existing parent decides.
    """
    entry = mount_for_path(path, read_mounts(mountinfo))
    return entry.mount_point if entry is not None and entry.read_only else None


_CONTAINER_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/")


def own_container_id(mountinfo: Path = MOUNTINFO) -> str | None:
    """This container's full ID, from the bind mounts Docker adds for
    /etc/hostname, /etc/hosts and /etc/resolv.conf."""
    for entry in read_mounts(mountinfo):
        if entry.mount_point in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):
            match = _CONTAINER_ID_RE.search(entry.root)
            if match:
                return match.group(1)
    return None


def host_path_for(path: str, docker_client: Any, mountinfo: Path = MOUNTINFO) -> str | None:
    """Translate a path inside this container to the Docker host's path.

    Inspects this container through the Docker socket and maps ``path``
    through the mount with the longest matching destination. Returns None
    when not in a container, Docker is unavailable, or ``path`` isn't on a
    mount. Blocking (Docker API call).
    """
    if docker_client is None:
        return None
    container_id = own_container_id(mountinfo)
    if not container_id:
        return None
    try:
        attrs = docker_client.containers.get(container_id).attrs or {}
    except Exception:
        return None
    normalized = os.path.normpath(path)
    best: tuple[str, str] | None = None
    for mount in attrs.get("Mounts") or []:
        dest = os.path.normpath(str(mount.get("Destination") or ""))
        source = str(mount.get("Source") or "")
        if not dest or not source:
            continue
        if normalized == dest or normalized.startswith(dest.rstrip("/") + "/"):
            if best is None or len(dest) > len(best[0]):
                best = (dest, source)
    if best is None:
        return None
    dest, source = best
    remainder = os.path.relpath(normalized, dest)
    return source if remainder == "." else os.path.join(source, remainder)
