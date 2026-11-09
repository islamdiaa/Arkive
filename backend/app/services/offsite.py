"""3-2-1 awareness: does any copy of the backups leave this server?

A backup kept only on the machine it protects is lost along with that machine:
a dead disk controller, a fire, theft, ransomware. ``evaluate_offsite`` looks
at the enabled storage targets and reports when every one of them is on this
server — a ``local`` target (unless its folder is a network share), or an
SFTP/S3 target whose host points back here (loopback, this server's host name
or addresses, the Docker host, a container on this Docker daemon).

This is a warning, never an error: it describes where backups go, not whether
they happen. Detection errs towards silence — a target counts as on this
server only when that is evident, so a remote host Arkive can't place is
treated as offsite.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite

from app.core.security import decrypt_config
from app.services.host_identity import _unraid_server_name
from app.services.settings_store import get_setting
from app.utils.mounts import MOUNTINFO, MountEntry, mount_for_path, read_mounts

logger = logging.getLogger("arkive.offsite")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Hosted services: always someone else's machine.
CLOUD_TARGET_TYPES = frozenset({"b2", "dropbox", "gdrive", "wasabi"})
# Self-hostable protocols: on this server when their host points back here.
HOST_FIELDS = {"sftp": "host", "s3": "endpoint"}

# A ``local`` target on one of these lives on another machine.
NETWORK_FSTYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "fuse.sshfs",
        "fuse.rclone",
        "fuse.s3fs",
        "davfs",
        "fuse.davfs2",
        "ceph",
        "glusterfs",
        "fuse.glusterfs",
    }
)

DOCKER_HOST_ALIASES = frozenset({"host.docker.internal", "gateway.docker.internal"})
# Domains a LAN uses for its own machines: "tower.local" is the server "tower".
LAN_DOMAINS = frozenset({"local", "lan", "home", "internal", "localdomain", "home.arpa"})

PROC_NET = Path("/proc/net")
BOOT_CONFIG = Path("/boot-config")
IDENTITY_TTL_SECONDS = 600
IDENTITY_TIMEOUT_SECONDS = 5.0

NO_OFFSITE_MESSAGE = (
    "Every enabled storage target is on this server, so there is no offsite copy. "
    "A disk failure, fire, theft or ransomware here would take the backups with it. "
    "Add a target on another machine or in the cloud."
)


@dataclass(frozen=True)
class LocalIdentity:
    """Names and addresses that reach this server."""

    names: frozenset[str] = frozenset()
    addresses: frozenset[IPAddress] = frozenset()
    containers: frozenset[str] = frozenset()


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().rstrip(".").lower()


def name_variants(value: Any) -> set[str]:
    """A host name plus its short form when its domain is a LAN one."""
    name = _normalize_name(value)
    if not name:
        return set()
    variants = {name}
    first, _, domain = name.partition(".")
    if domain in LAN_DOMAINS:
        variants.add(first)
    return variants


def parse_ip(value: Any) -> IPAddress | None:
    text = str(value or "").strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


# ---------------------------------------------------------------------------
# This server's identity
# ---------------------------------------------------------------------------


def _interface_addresses(proc_net: Path) -> set[IPAddress]:
    """Addresses of this network namespace's interfaces (no extra dependencies)."""
    found: set[IPAddress] = set()
    try:
        last = ""
        for line in (proc_net / "fib_trie").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("|--"):
                last = stripped[3:].strip()
            elif "/32 host LOCAL" in stripped and (ip := parse_ip(last)):
                found.add(ip)
    except OSError:
        pass
    try:
        for line in (proc_net / "if_inet6").read_text().splitlines():
            raw = line.split()[0] if line.split() else ""
            if len(raw) == 32 and (ip := parse_ip(":".join(raw[i : i + 4] for i in range(0, 32, 4)))):
                found.add(ip)
    except OSError:
        pass
    return found


_UNRAID_IP_RE = re.compile(r'^IPADDR6?[^=\n]*="?([0-9A-Fa-f:.]+)', re.MULTILINE)


def _unraid_addresses(boot_config: Path) -> set[IPAddress]:
    """Static addresses from Unraid's flash network config (DHCP leaves none)."""
    try:
        text = (boot_config / "network.cfg").read_text(errors="replace")
    except OSError:
        return set()
    return {ip for match in _UNRAID_IP_RE.finditer(text) if (ip := parse_ip(match.group(1)))}


def _docker_identity(docker_client: Any) -> tuple[set[str], set[IPAddress]]:
    """The Docker host's name, and its address on every bridge network.

    Only ``bridge`` networks: a macvlan/ipvlan gateway is the LAN router.
    """
    names: set[str] = set()
    addresses: set[IPAddress] = set()
    if docker_client is None:
        return names, addresses
    try:
        names |= name_variants((docker_client.info() or {}).get("Name"))
    except Exception as exc:
        logger.debug("Offsite check: no Docker host name: %s", exc)
    try:
        for network in docker_client.networks.list():
            attrs = getattr(network, "attrs", None) or {}
            if attrs.get("Driver") != "bridge":
                continue
            for ipam in (attrs.get("IPAM") or {}).get("Config") or []:
                if isinstance(ipam, dict) and (ip := parse_ip(ipam.get("Gateway"))):
                    addresses.add(ip)
    except Exception as exc:
        logger.debug("Offsite check: no Docker networks: %s", exc)
    return names, addresses


def gather_system_identity(docker_client: Any = None) -> LocalIdentity:
    """Blocking: read interfaces, Unraid config and the Docker daemon."""
    names = name_variants(socket.gethostname()) | name_variants(_unraid_server_name(BOOT_CONFIG / "ident.cfg"))
    addresses = _interface_addresses(PROC_NET) | _unraid_addresses(BOOT_CONFIG)
    docker_names, docker_addresses = _docker_identity(docker_client)
    return LocalIdentity(names=frozenset(names | docker_names), addresses=frozenset(addresses | docker_addresses))


_identity_lock = threading.Lock()
_identity_cache: tuple[float, LocalIdentity] | None = None


def cached_system_identity(docker_client: Any = None) -> LocalIdentity | None:
    """Blocking, cached. Only one gather runs at a time, so a hung Docker
    daemon ties up one worker thread rather than one per status poll."""
    global _identity_cache
    cached = _identity_cache
    if cached and time.monotonic() - cached[0] < IDENTITY_TTL_SECONDS:
        return cached[1]
    if not _identity_lock.acquire(blocking=False):
        return cached[1] if cached else None
    try:
        identity = gather_system_identity(docker_client)
        _identity_cache = (time.monotonic(), identity)
        return identity
    finally:
        _identity_lock.release()


def reset_identity_cache() -> None:
    global _identity_cache
    _identity_cache = None


async def _container_names(db: aiosqlite.Connection) -> set[str]:
    """Containers on this Docker daemon, plus their compose service names."""
    try:
        cursor = await db.execute("SELECT name, compose_project FROM discovered_containers")
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return set()
    names: set[str] = set()
    for row in rows:
        name, project = _normalize_name(row[0]), _normalize_name(row[1])
        if not name:
            continue
        names.add(name)
        if project and (match := re.fullmatch(re.escape(project) + r"[-_](.+)[-_]\d+", name)):
            names.add(match.group(1))
    return names


async def local_identity(db: aiosqlite.Connection, docker_client: Any = None) -> LocalIdentity:
    try:
        system = await asyncio.wait_for(
            asyncio.to_thread(cached_system_identity, docker_client), IDENTITY_TIMEOUT_SECONDS
        )
    except Exception as exc:  # includes TimeoutError: a slow daemon must not stall /api/status
        logger.debug("Offsite check: identity unavailable: %s", exc)
        system = _identity_cache[1] if _identity_cache else None
    system = system or LocalIdentity()
    return LocalIdentity(
        names=system.names | name_variants(await get_setting(db, "server_name", "")),
        addresses=system.addresses,
        containers=frozenset(await _container_names(db)),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def target_host(target_type: str, config: dict) -> str:
    """The host an SFTP/S3 target connects to, or '' when there is none."""
    field = HOST_FIELDS.get(target_type)
    raw = str((config or {}).get(field) or "").strip() if field else ""
    if not raw:
        return ""
    if parse_ip(raw):
        return raw.strip("[]")
    if target_type == "s3" or "://" in raw:
        try:
            return urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
        except ValueError:
            return ""
    if raw.startswith("["):
        return raw[1:].split("]", 1)[0]
    return raw.split(":", 1)[0] if raw.count(":") == 1 else raw


def host_locality(host: str, identity: LocalIdentity) -> str | None:
    """Why ``host`` is this server, or None when that isn't evident."""
    ip = parse_ip(host)
    if ip is not None:
        if ip.is_loopback or ip.is_unspecified:
            return f"{host} is a loopback address"
        if ip in identity.addresses:
            return f"{host} is an address of this server"
        return None
    name = _normalize_name(host)
    if not name:
        return None
    if name == "localhost" or name.endswith(".localhost"):
        return f"{host} is this server (localhost)"
    if name in DOCKER_HOST_ALIASES:
        return f"{host} is the Docker host"
    variants = name_variants(name)
    if variants & identity.names:
        return f"{host} is this server's host name"
    if variants & identity.containers:
        return f"{host} is a container on this server"
    return None


def local_path_locality(path: str, mounts: list[MountEntry]) -> str | None:
    """Why a ``local`` target is on this server, or None for a network share."""
    entry = mount_for_path(path, mounts) if path else None
    if entry is not None and entry.fstype in NETWORK_FSTYPES:
        return None
    return f"{path} is a folder on this server" if path else "a folder on this server"


def _read_config(raw: Any, config_dir: str | Path | None) -> dict:
    text = str(raw or "")
    if not text:
        return {}
    try:
        if text.startswith("enc:v1:"):
            # Never let a missing keyfile be created as a side effect here.
            if config_dir is None or not (Path(config_dir) / ".keyfile").is_file():
                return {}
            return decrypt_config(text, str(config_dir))
        return decrypt_config(text)
    except Exception as exc:
        logger.debug("Offsite check: cannot read target config: %s", exc)
        return {}


async def evaluate_offsite(
    db: aiosqlite.Connection,
    *,
    config_dir: str | Path | None = None,
    docker_client: Any = None,
    mountinfo: Path | None = None,
) -> dict:
    """Is any enabled storage target off this server?

    Returns ``{"has_offsite_copy", "warning", "enabled_targets",
    "offsite_targets", "local_targets": [{"id", "name", "type", "reason"}],
    "message"}``. ``warning`` is True when targets are enabled and every one
    of them is on this server. With no enabled targets there is no warning
    here: runs already turn partial for that.
    """
    result: dict = {
        "has_offsite_copy": False,
        "warning": False,
        "enabled_targets": 0,
        "offsite_targets": 0,
        "local_targets": [],
        "message": None,
    }
    try:
        cursor = await db.execute(
            "SELECT id, name, type, config FROM storage_targets WHERE enabled = 1 ORDER BY created_at, id"
        )
        rows = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return result

    identity: LocalIdentity | None = None
    mounts: list[MountEntry] | None = None
    for row in rows:
        target_id, name, target_type, raw_config = row[0], row[1], str(row[2] or ""), row[3]
        reason: str | None = None
        if target_type == "local":
            if mounts is None:
                mounts = read_mounts(mountinfo or MOUNTINFO)
            path = str(_read_config(raw_config, config_dir).get("path") or "")
            reason = local_path_locality(path, mounts)
        elif target_type in HOST_FIELDS:
            host = target_host(target_type, _read_config(raw_config, config_dir))
            if host:
                if identity is None:
                    identity = await local_identity(db, docker_client)
                reason = host_locality(host, identity)
        # Cloud providers and unknown types are not on this server.

        result["enabled_targets"] += 1
        if reason:
            result["local_targets"].append({"id": target_id, "name": name, "type": target_type, "reason": reason})
        else:
            result["offsite_targets"] += 1

    result["has_offsite_copy"] = result["offsite_targets"] > 0
    result["warning"] = result["enabled_targets"] > 0 and not result["has_offsite_copy"]
    if result["warning"]:
        result["message"] = NO_OFFSITE_MESSAGE
    return result


def docker_client_for(app: Any) -> Any:
    """The Docker client the app runs with, if any (same lookup as host_identity)."""
    state = getattr(app, "state", None)
    return getattr(getattr(state, "discovery", None), "docker", None) or getattr(state, "docker_client", None)
