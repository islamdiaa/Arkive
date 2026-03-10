"""Helpers for resolving the real server identity outside the container."""

from __future__ import annotations

import logging
import socket
from typing import Any

logger = logging.getLogger("arkive.host_identity")


def resolve_hostname(*, app: Any | None = None, settings: dict[str, str] | None = None) -> str:
    """Resolve the best available server hostname.

    Preference order:
    1. user-configured server_name setting
    2. Docker daemon host name via mounted Docker socket
    3. container hostname fallback
    """
    configured_name = (settings or {}).get("server_name", "").strip()
    if configured_name:
        return configured_name

    discovery = getattr(getattr(app, "state", None), "discovery", None)
    docker_client = getattr(discovery, "docker", None)
    if docker_client is not None:
        try:
            info = docker_client.info()
            daemon_name = str(info.get("Name", "") or "").strip()
            if daemon_name:
                return daemon_name
        except Exception as exc:
            logger.debug("Unable to resolve Docker daemon hostname: %s", exc)

    return socket.gethostname()
