"""Helpers for resolving the real server identity outside the container."""

from __future__ import annotations

import logging
import re
import socket
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("arkive.host_identity")

UNRAID_IDENT_CFG = Path("/boot-config/ident.cfg")
_IDENT_NAME_RE = re.compile(r'^NAME="?([^"\n]+)"?\s*$', re.MULTILINE)

# Last host name obtained from the Docker daemon. Restic groups snapshots for
# retention by host name, so a container ID leaking in while the daemon is
# briefly unreachable splits the retention history.
_last_daemon_name: str | None = None
_last_daemon_name_at = 0.0
# The daemon name practically never changes; don't hit `docker info` (a
# blocking call) on every status request.
DAEMON_NAME_TTL_SECONDS = 600


def _unraid_server_name(ident_cfg: Path) -> str:
    """Read the server name from Unraid's flash config (ident.cfg)."""
    try:
        match = _IDENT_NAME_RE.search(ident_cfg.read_text(errors="replace"))
    except OSError:
        return ""
    return match.group(1).strip() if match else ""


def resolve_hostname(
    *,
    app: Any | None = None,
    settings: dict[str, str] | None = None,
    docker_client: Any | None = None,
    ident_cfg: Path | None = None,
) -> str:
    """Resolve the best available server hostname.

    Preference order:
    1. user-configured server_name setting
    2. Docker daemon host name via mounted Docker socket
    3. last daemon host name seen by this process
    4. Unraid server name from /boot-config/ident.cfg
    5. container hostname fallback
    """
    global _last_daemon_name, _last_daemon_name_at
    configured_name = (settings or {}).get("server_name", "").strip()
    if configured_name:
        return configured_name

    resolved_docker_client = docker_client
    if resolved_docker_client is None:
        discovery = getattr(getattr(app, "state", None), "discovery", None)
        resolved_docker_client = getattr(discovery, "docker", None)

    if resolved_docker_client is None:
        direct_client = getattr(getattr(app, "state", None), "docker_client", None)
        resolved_docker_client = direct_client

    if _last_daemon_name and time.monotonic() - _last_daemon_name_at < DAEMON_NAME_TTL_SECONDS:
        return _last_daemon_name

    if resolved_docker_client is not None:
        try:
            info = resolved_docker_client.info()
            daemon_name = str(info.get("Name", "") or "").strip()
            if daemon_name:
                _last_daemon_name = daemon_name
                _last_daemon_name_at = time.monotonic()
                return daemon_name
        except Exception as exc:
            logger.debug("Unable to resolve Docker daemon hostname: %s", exc)

    if _last_daemon_name:
        return _last_daemon_name

    unraid_name = _unraid_server_name(ident_cfg or UNRAID_IDENT_CFG)
    if unraid_name:
        return unraid_name

    return socket.gethostname()


def reset_hostname_cache() -> None:
    """Forget the cached daemon host name (tests, settings changes)."""
    global _last_daemon_name, _last_daemon_name_at
    _last_daemon_name = None
    _last_daemon_name_at = 0.0
