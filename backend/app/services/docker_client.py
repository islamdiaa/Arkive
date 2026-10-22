"""Resilient Docker client wrapper.

On a busy Unraid host the Docker daemon can take minutes to answer right after
boot. docker-py's ``from_env()`` performs an API version handshake at
construction time, so a single timeout during Arkive startup used to leave the
app without a Docker client until the next container restart — every scheduled
backup then silently skipped discovery and database dumps.

``ResilientDockerClient`` is a drop-in proxy for ``docker.DockerClient``: the
real client is created lazily on first use and re-created whenever creation
failed, so a transient daemon outage only affects the operations that run
while the daemon is actually down.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("arkive.docker_client")

DEFAULT_TIMEOUT_SECONDS = 120
# Minimum delay between connection attempts so a dead daemon doesn't get
# hammered by every API request that touches Docker.
RECONNECT_BACKOFF_SECONDS = 10.0


class DockerUnavailableError(RuntimeError):
    """Raised when the Docker daemon cannot be reached."""


def _default_factory(timeout: int) -> Any:
    import docker

    return docker.from_env(timeout=timeout)


class ResilientDockerClient:
    """Lazily-connecting, self-healing proxy around ``docker.DockerClient``."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        factory: Callable[[int], Any] | None = None,
        backoff_seconds: float = RECONNECT_BACKOFF_SECONDS,
    ):
        self._timeout = timeout
        self._factory = factory or _default_factory
        self._backoff = backoff_seconds
        self._client: Any | None = None
        self._lock = threading.Lock()
        self._last_attempt = 0.0
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def connected(self) -> bool:
        return self._client is not None

    def get_client(self) -> Any:
        """Return the underlying client, connecting if needed.

        Raises DockerUnavailableError if the daemon cannot be reached.
        """
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._client is not None:
                return self._client
            now = time.monotonic()
            if self._last_error and now - self._last_attempt < self._backoff:
                raise DockerUnavailableError(f"Docker daemon unavailable: {self._last_error}")
            self._last_attempt = now
            try:
                self._client = self._factory(self._timeout)
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Docker daemon unavailable: %s", exc)
                raise DockerUnavailableError(f"Docker daemon unavailable: {exc}") from exc
            if self._last_error:
                logger.info("Docker daemon connection re-established")
            self._last_error = None
            return self._client

    def reset(self) -> None:
        """Drop the current client so the next call reconnects."""
        with self._lock:
            client, self._client = self._client, None
            self._last_attempt = 0.0
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.debug("Error closing Docker client: %s", exc)

    def ping(self) -> bool:
        """Return True if the daemon answers, reconnecting if necessary."""
        try:
            return bool(self.get_client().ping())
        except Exception as exc:
            logger.debug("Docker ping failed: %s", exc)
            return False

    def close(self) -> None:
        self.reset()

    def __getattr__(self, name: str) -> Any:
        # Only called for attributes not defined on the proxy itself.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.get_client(), name)
