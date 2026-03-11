"""Helpers for resolving the real server identity outside the container."""

from __future__ import annotations

import logging
import socket
from typing import Any

logger = logging.getLogger("arkive.host_identity")


def resolve_hostname(
    *,
    app: Any | None = None,
    settings: dict[str, str] | None = None,
    docker_client: Any | None = None,
) -> str:
    """Resolve the best available server hostname.

    Preference order:
    1. user-configured server_name setting
    2. Docker daemon host name via mounted Docker socket
    3. container hostname fallback
    """
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

    if resolved_docker_client is not None:
        try:
            info = resolved_docker_client.info()
            daemon_name = str(info.get("Name", "") or "").strip()
            if daemon_name:
                return daemon_name
        except Exception as exc:
            logger.debug("Unable to resolve Docker daemon hostname: %s", exc)

    return socket.gethostname()
