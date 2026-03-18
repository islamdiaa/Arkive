"""FastAPI dependencies for DB, config, auth, and service access."""

import inspect
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
from fastapi import Depends, HTTPException, Request

from app.core.config import ArkiveConfig
from app.core.exceptions import AuthError, RateLimitError
from app.core.security import BROWSER_SESSION_COOKIE, verify_api_key, verify_browser_session, verify_sse_token

logger = logging.getLogger("arkive.auth")

_config = ArkiveConfig()

# Rate limiting state
_rate_limit_lock = threading.Lock()
_failed_attempts: dict[str, list[float]] = defaultdict(list)
_lockouts: dict[str, float] = {}
RATE_LIMIT_WINDOW = 300  # 5 minutes
RATE_LIMIT_MAX = 5
LOCKOUT_DURATION = 60  # seconds
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def get_db():
    """Yield async SQLite connection."""
    async with aiosqlite.connect(_config.db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db


def get_config() -> ArkiveConfig:
    """Return ArkiveConfig singleton."""
    return _config


def _track_failed_attempt(ip: str) -> None:
    """Track failed auth attempt for rate limiting."""
    with _rate_limit_lock:
        now = time.time()
        _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < RATE_LIMIT_WINDOW]
        _failed_attempts[ip].append(now)
        if len(_failed_attempts[ip]) >= RATE_LIMIT_MAX:
            _lockouts[ip] = now + LOCKOUT_DURATION
            logger.warning("Rate limiting IP %s after %d failed attempts", ip, RATE_LIMIT_MAX)


def _is_locked_out(ip: str) -> bool:
    """Check if IP is currently locked out."""
    with _rate_limit_lock:
        lockout_until = _lockouts.get(ip, 0)
        if time.time() < lockout_until:
            return True
        _lockouts.pop(ip, None)
        return False


def clear_rate_limit(ip: str) -> None:
    """Clear rate limit state for an IP (e.g. after successful setup)."""
    with _rate_limit_lock:
        _failed_attempts.pop(ip, None)
        _lockouts.pop(ip, None)


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")

    referer = request.headers.get("referer")
    if not referer:
        return None
    parsed = urlsplit(referer)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _enforce_session_origin(request: Request) -> None:
    if request.method.upper() not in _UNSAFE_METHODS:
        return

    allowed = {str(request.base_url).rstrip("/"), *_config.cors_origins}
    origin = _request_origin(request)
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="Cross-site cookie-authenticated request blocked")


async def require_auth(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    """Validate API key from X-API-Key header. Raises 401 if invalid."""
    client_ip = request.client.host if request.client else "unknown"

    if _is_locked_out(client_ip):
        raise RateLimitError("Too many failed attempts. Try again later.", retry_after=LOCKOUT_DURATION)

    # Setup mode bypass
    try:
        row = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        result = await row.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return  # Schema still bootstrapping — treat as setup mode
    if not result:
        return  # No API key set yet — setup mode

    api_key = request.headers.get("X-API-Key")
    if api_key:
        if not verify_api_key(api_key, result["value"]):
            _track_failed_attempt(client_ip)
            raise AuthError("Invalid API key")
        clear_rate_limit(client_ip)
        return

    session_token = request.cookies.get(BROWSER_SESSION_COOKIE)
    if session_token and verify_browser_session(session_token, result["value"]):
        _enforce_session_origin(request)
        clear_rate_limit(client_ip)
        return

    raise AuthError("Missing API key (X-API-Key header required)")


async def require_sse_auth(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    """Validate short-lived SSE token from ?token= query param."""
    # Check setup mode
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        result = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return
    if not result:
        return  # setup not complete

    token = request.query_params.get("token")
    if not token:
        raise AuthError("Missing SSE token (?token= required)")
    if not verify_sse_token(token):
        raise AuthError("Invalid or expired SSE token")


# Service accessors
def get_orchestrator(request: Request) -> Any:
    return request.app.state.orchestrator


def get_event_bus(request: Request) -> Any:
    bus = request.app.state.event_bus
    if bus is None:
        return None

    class _EventBusAdapter:
        def __init__(self, inner: Any):
            self._inner = inner

        async def publish(self, event_type: str, data: Any) -> None:
            publish = getattr(self._inner, "publish", None)
            if not callable(publish):
                return
            result = publish(event_type, data)
            if inspect.isawaitable(result):
                await result

        def subscribe(self) -> Any:
            return self._inner.subscribe()

        def unsubscribe(self, q: Any) -> None:
            self._inner.unsubscribe(q)

    return _EventBusAdapter(bus)


def get_scheduler(request: Request) -> Any:
    return request.app.state.scheduler


def get_discovery(request: Request) -> Any:
    return request.app.state.discovery


def get_db_dumper(request: Request) -> Any:
    return request.app.state.db_dumper


def get_backup_engine(request: Request) -> Any:
    return request.app.state.backup_engine


def get_cloud_manager(request: Request) -> Any:
    return request.app.state.cloud_manager


def get_notifier(request: Request) -> Any:
    return request.app.state.notifier


def get_restore_plan(request: Request) -> Any:
    return request.app.state.restore_plan


def get_verify_engine(request: Request) -> Any:
    return getattr(request.app.state, "verify_engine", None)
