"""3-2-1 awareness: does any copy of the backups leave this server?

A backup kept only on the machine it protects is lost along with that machine:
a dead disk controller, a fire, theft, ransomware. ``evaluate_offsite`` looks
at the storage targets the enabled backup jobs upload to and reports when
every one of them is on this server — a ``local`` target (unless its folder is
a network share), or an SFTP/S3 target whose host points back here (loopback,
this server's host name or addresses, the Docker host, a container on this
Docker daemon).

It follows the orchestrator: a job whose ``targets`` list names targets
uploads to those only; an empty list means every enabled target. An enabled
offsite target that no enabled job uploads to is reported, but it is not an
offsite copy.

This is a warning, never an error: it describes where backups go, not whether
they happen. Detection errs towards silence — a target counts as on this
server only when that is evident, so a host that resolves to an address Arkive
can't place is treated as offsite. A lookup that fails (Docker unreachable,
DNS down, a config that can't be read) is not an answer: the result is
``uncertain`` and the last verdict stands instead of being reset.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
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
# Docker network drivers whose endpoints are containers on this host.
LOCAL_NETWORK_DRIVERS = frozenset({"bridge", "macvlan", "ipvlan"})
# Unraid reserves the server's own address on its custom networks (br0) under
# this auxiliary-address name, so it is known even when the server uses DHCP.
UNRAID_SERVER_AUX_ADDRESS = "server"

PROC_NET = Path("/proc/net")
BOOT_CONFIG = Path("/boot-config")
IDENTITY_TTL_SECONDS = 600
# A gather that could not reach Docker is retried sooner.
INCOMPLETE_IDENTITY_TTL_SECONDS = 60
IDENTITY_TIMEOUT_SECONDS = 5.0

# "No offsite copy" is told once per episode, across restarts (scheduler):
# when it started (activity entry written) and when a channel received the
# notification. An uncertain evaluation keeps the episode open.
OFFSITE_WARNING_SINCE_KEY = "offsite_warning_since"
OFFSITE_NOTIFIED_AT_KEY = "offsite_warning_notified_at"

NO_OFFSITE_MESSAGE = (
    "Every storage target the enabled backup jobs upload to is on this server, so there is no offsite copy. "
    "A disk failure, fire, theft or ransomware here would take the backups with it."
)
ADD_TARGET_ADVICE = "Add a target on another machine or in the cloud."


@dataclass(frozen=True)
class LocalIdentity:
    """Names and addresses that reach this server."""

    names: frozenset[str] = frozenset()
    addresses: frozenset[IPAddress] = frozenset()
    # Address of a container on this Docker daemon -> the container's name.
    containers: Mapping[IPAddress, str] = field(default_factory=dict)
    # Target host name -> the addresses it resolved to (absent: lookup failed).
    resolved: Mapping[str, frozenset[IPAddress]] = field(default_factory=dict)
    # False when Docker could not be asked and nothing was known from before.
    complete: bool = True


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


def _docker_identity(docker_client: Any) -> tuple[set[str], set[IPAddress], dict[IPAddress, str], bool]:
    """The Docker host's names and addresses, and the addresses of its containers.

    A bridge network's gateway is this server; a macvlan/ipvlan gateway is the
    LAN router, but Unraid records the server's own address on those networks
    as an auxiliary address. The last value is False when the daemon could
    not be asked, so the caller can tell "nothing found" from "didn't look".
    Without a Docker socket there is nothing to ask, which is not a failure.
    """
    names: set[str] = set()
    addresses: set[IPAddress] = set()
    containers: dict[IPAddress, str] = {}
    if docker_client is None:
        return names, addresses, containers, True
    complete = True
    try:
        names |= name_variants((docker_client.info() or {}).get("Name"))
    except Exception as exc:
        complete = False
        logger.debug("Offsite check: no Docker host name: %s", exc)
    try:
        # greedy: inspect each network, which is what lists its containers.
        for network in docker_client.networks.list(greedy=True):
            attrs = getattr(network, "attrs", None) or {}
            driver = attrs.get("Driver")
            if driver not in LOCAL_NETWORK_DRIVERS:
                continue
            for ipam in (attrs.get("IPAM") or {}).get("Config") or []:
                if not isinstance(ipam, dict):
                    continue
                if driver == "bridge" and (ip := parse_ip(ipam.get("Gateway"))):
                    addresses.add(ip)
                aux = ipam.get("AuxiliaryAddresses")
                if isinstance(aux, dict) and (ip := parse_ip(aux.get(UNRAID_SERVER_AUX_ADDRESS))):
                    addresses.add(ip)
            for endpoint in (attrs.get("Containers") or {}).values():
                if not isinstance(endpoint, dict):
                    continue
                for key in ("IPv4Address", "IPv6Address"):
                    if ip := parse_ip(str(endpoint.get(key) or "").split("/", 1)[0]):
                        containers[ip] = str(endpoint.get("Name") or "").lstrip("/") or "a container"
    except Exception as exc:
        complete = False
        logger.debug("Offsite check: no Docker networks: %s", exc)
    return names, addresses, containers, complete


# Last answers that worked, used while Docker or DNS is briefly unreachable
# (host_identity keeps the daemon name the same way). Written only by the
# gather, which runs one at a time.
_last_docker: tuple[set[str], set[IPAddress], dict[IPAddress, str]] | None = None
_last_resolved: dict[str, frozenset[IPAddress]] = {}


def _resolve(host: str) -> frozenset[IPAddress] | None:
    """Blocking: the addresses ``host`` resolves to from here, as restic and
    rclone would see them (Docker's DNS first, then the LAN's). Falls back to
    the last answer; None when the lookup failed and there is none."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        logger.debug("Offsite check: cannot resolve %s: %s", host, exc)
        return _last_resolved.get(host)
    found = frozenset(ip for info in infos if (ip := parse_ip(info[4][0])))
    if not found:
        return _last_resolved.get(host)
    _last_resolved[host] = found
    return found


def gather_system_identity(docker_client: Any = None, hosts: Iterable[str] = ()) -> LocalIdentity:
    """Blocking: read interfaces, Unraid config and the Docker daemon, and look up ``hosts``."""
    global _last_docker
    unraid_name = _unraid_server_name(BOOT_CONFIG / "ident.cfg")
    names = name_variants(socket.gethostname()) | name_variants(unraid_name)
    addresses = _interface_addresses(PROC_NET) | _unraid_addresses(BOOT_CONFIG)
    docker_names, docker_addresses, containers, complete = _docker_identity(docker_client)
    if complete:
        _last_docker = (set(docker_names), set(docker_addresses), dict(containers))
    elif _last_docker is not None:
        # A Docker hiccup is not news: what the daemon said last still holds.
        docker_names |= _last_docker[0]
        docker_addresses |= _last_docker[1]
        containers = {**_last_docker[2], **containers}
        complete = True
    names |= docker_names
    addresses |= docker_addresses
    # A DHCP address is in no config file; the server's own names find it.
    # Only names that are this machine's (not the user's display label), and
    # only non-public answers: a search domain must not adopt someone's server.
    for name in name_variants(unraid_name) | docker_names:
        addresses |= {ip for ip in _resolve(name) or () if not ip.is_global}
    resolved = {host: ips for host in hosts if (ips := _resolve(host)) is not None}
    return LocalIdentity(
        names=frozenset(names),
        addresses=frozenset(addresses),
        containers=containers,
        resolved=resolved,
        complete=complete,
    )


_identity_lock = threading.Lock()
# (expires at, hosts looked up, identity)
_identity_cache: tuple[float, frozenset[str], LocalIdentity] | None = None


def cached_system_identity(docker_client: Any = None, hosts: Iterable[str] = ()) -> LocalIdentity | None:
    """Blocking, cached. Only one gather runs at a time, so a hung Docker
    daemon or DNS server ties up one worker thread rather than one per
    status poll."""
    global _identity_cache
    wanted = frozenset(hosts)
    cached = _identity_cache
    if cached and time.monotonic() < cached[0] and wanted <= cached[1]:
        return cached[2]
    if not _identity_lock.acquire(blocking=False):
        return cached[2] if cached else None
    try:
        identity = gather_system_identity(docker_client, wanted)
        ttl = IDENTITY_TTL_SECONDS if identity.complete else INCOMPLETE_IDENTITY_TTL_SECONDS
        _identity_cache = (time.monotonic() + ttl, wanted, identity)
        return identity
    finally:
        _identity_lock.release()


def reset_identity_cache() -> None:
    """Forget cached and last-known answers (tests)."""
    global _identity_cache, _last_docker
    _identity_cache = None
    _last_docker = None
    _last_resolved.clear()


async def local_identity(
    db: aiosqlite.Connection, docker_client: Any = None, hosts: Iterable[str] = ()
) -> LocalIdentity:
    try:
        system = await asyncio.wait_for(
            asyncio.to_thread(cached_system_identity, docker_client, frozenset(hosts)), IDENTITY_TIMEOUT_SECONDS
        )
    except Exception as exc:  # includes TimeoutError: a slow daemon must not stall /api/status
        logger.debug("Offsite check: identity unavailable: %s", exc)
        system = _identity_cache[2] if _identity_cache else None
    system = system or LocalIdentity(complete=False)
    return replace(system, names=system.names | name_variants(await get_setting(db, "server_name", "")))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def target_host(target_type: str, config: dict) -> str:
    """The host an SFTP/S3 target connects to, or '' when there is none."""
    field_name = HOST_FIELDS.get(target_type)
    raw = str((config or {}).get(field_name) or "").strip() if field_name else ""
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


def _address_locality(ip: IPAddress, identity: LocalIdentity) -> str | None:
    if ip.is_loopback or ip.is_unspecified:
        return "a loopback address"
    if ip in identity.addresses:
        return "an address of this server"
    if container := identity.containers.get(ip):
        return f"the container {container} on this server"
    return None


def host_locality(host: str, identity: LocalIdentity) -> str | None:
    """Why ``host`` is this server, or None when that isn't evident.

    A name counts by what it resolves to from here, not by looking like a
    container's name: Docker's DNS answers container names only on networks
    Arkive shares with them, and anything else is another machine's to answer.
    """
    ip = parse_ip(host)
    if ip is not None:
        what = _address_locality(ip, identity)
        return f"{host} is {what}" if what else None
    name = _normalize_name(host)
    if not name:
        return None
    if name == "localhost" or name.endswith(".localhost"):
        return f"{host} is this server (localhost)"
    if name in DOCKER_HOST_ALIASES:
        return f"{host} is the Docker host"
    if name_variants(name) & identity.names:
        return f"{host} is this server's host name"
    for resolved_ip in sorted(identity.resolved.get(name, ()), key=str):
        if what := _address_locality(resolved_ip, identity):
            return f"{host} resolves to {resolved_ip}, {what}"
    return None


def host_is_placed(host: str, identity: LocalIdentity) -> bool:
    """Whether ``host_locality`` returning None is an answer ("not here")
    rather than missing information (Docker or DNS couldn't be asked)."""
    if not identity.complete:
        return False
    return parse_ip(host) is not None or _normalize_name(host) in identity.resolved


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


def _job_target_ids(raw: Any) -> list[str] | None:
    """A job's pinned target IDs ([] = every enabled target), None if unreadable."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    ids = []
    for entry in value:
        target_id = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(target_id, str) and target_id:
            ids.append(target_id)
    return ids


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _warning_message(unused: list[dict], pinned_jobs: list[str]) -> str:
    if not unused:
        return f"{NO_OFFSITE_MESSAGE} {ADD_TARGET_ADVICE}"
    verb, pronoun = ("is", "it") if len(unused) == 1 else ("are", "them")
    message = (
        f"{NO_OFFSITE_MESSAGE} {_names([str(t['name']) for t in unused])} {verb} enabled, "
        f"but no enabled backup job uploads to {pronoun}"
    )
    if pinned_jobs:
        uploads, whom = ("uploads", "it") if len(pinned_jobs) == 1 else ("upload", "them")
        message += f": {_names(pinned_jobs)} {uploads} only to the targets chosen for {whom}"
    return f"{message}. A job created in Settings > Jobs uploads to every enabled target."


async def evaluate_offsite(
    db: aiosqlite.Connection,
    *,
    config_dir: str | Path | None = None,
    docker_client: Any = None,
    mountinfo: Path | None = None,
) -> dict:
    """Does any backup leave this server?

    Returns ``{"has_offsite_copy", "warning", "uncertain", "enabled_targets",
    "offsite_targets", "local_targets": [{"id", "name", "type", "reason"}],
    "unused_offsite_targets": [{"id", "name", "type"}], "message"}``.

    ``local_targets`` and ``offsite_targets`` cover the targets the enabled
    jobs upload to. ``warning`` is True when they upload somewhere and all of
    it is on this server. With no destination there is no warning here: runs
    already turn partial for that. ``uncertain`` means a destination could not
    be placed because a lookup failed; ``warning`` then keeps the verdict of
    the episode the scheduler last recorded.
    """
    result: dict = {
        "has_offsite_copy": False,
        "warning": False,
        "uncertain": False,
        "enabled_targets": 0,
        "offsite_targets": 0,
        "local_targets": [],
        "unused_offsite_targets": [],
        "message": None,
    }
    try:
        cursor = await db.execute("SELECT id, name, type, config, enabled FROM storage_targets ORDER BY created_at, id")
        targets = {row[0]: row for row in await cursor.fetchall()}
        cursor = await db.execute("SELECT name, targets FROM backup_jobs WHERE enabled = 1 ORDER BY created_at, id")
        jobs = await cursor.fetchall()
    except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
        # e.g. "database is locked": not the same as "no targets".
        logger.debug("Offsite check: cannot read targets or jobs: %s", exc)
        result["uncertain"] = True
        return result

    # Where each enabled job uploads, resolved as the orchestrator does: named
    # targets whether or not they are enabled, else every enabled target.
    enabled = [target_id for target_id, row in targets.items() if row[4]]
    used: set[str] = set()
    pinned_jobs: list[str] = []
    for job_name, raw_targets in jobs:
        pinned = _job_target_ids(raw_targets)
        if pinned is None:
            continue
        if pinned:
            pinned_jobs.append(str(job_name))
        used.update(target_id for target_id in (pinned or enabled) if target_id in targets)

    considered = [target_id for target_id in targets if target_id in used or targets[target_id][4]]
    hosts: dict[str, str] = {}
    for target_id in considered:
        target_type = str(targets[target_id][2] or "")
        if target_type in HOST_FIELDS:
            hosts[target_id] = target_host(target_type, _read_config(targets[target_id][3], config_dir))
    identity: LocalIdentity | None = None
    if any(hosts.values()):
        lookups = {_normalize_name(host) for host in hosts.values() if host and parse_ip(host) is None}
        identity = await local_identity(db, docker_client, lookups)

    mounts: list[MountEntry] | None = None
    certain_offsite = 0
    for target_id in considered:
        _, name, raw_type, raw_config, _ = targets[target_id]
        target_type = str(raw_type or "")
        reason: str | None = None
        placed = True
        if target_type == "local":
            if mounts is None:
                mounts = read_mounts(mountinfo or MOUNTINFO)
            path = str(_read_config(raw_config, config_dir).get("path") or "")
            reason = local_path_locality(path, mounts)
        elif target_type in HOST_FIELDS:
            host = hosts.get(target_id, "")
            if not host or identity is None:
                placed = False  # no readable host: neither here nor evidently elsewhere
            else:
                reason = host_locality(host, identity)
                placed = reason is not None or host_is_placed(host, identity)
        # Cloud providers and unknown types are not on this server.

        info = {"id": target_id, "name": name, "type": target_type}
        if target_id in used:
            if reason:
                result["local_targets"].append({**info, "reason": reason})
            else:
                result["offsite_targets"] += 1
                certain_offsite += 1 if placed else 0
        elif not reason and placed:
            result["unused_offsite_targets"].append(info)

    result["enabled_targets"] = len(enabled)
    if certain_offsite:
        result["has_offsite_copy"] = True
    elif result["offsite_targets"]:
        # Something may be offsite but couldn't be checked: neither raise nor
        # clear the warning on missing information.
        result["uncertain"] = True
        result["warning"] = bool(await get_setting(db, OFFSITE_WARNING_SINCE_KEY, ""))
    elif result["local_targets"]:
        result["warning"] = True
    if result["warning"]:
        result["message"] = _warning_message(result["unused_offsite_targets"], pinned_jobs)
    return result


def docker_client_for(app: Any) -> Any:
    """The Docker client the app runs with, if any (same lookup as host_identity)."""
    state = getattr(app, "state", None)
    return getattr(getattr(state, "discovery", None), "docker", None) or getattr(state, "docker_client", None)
