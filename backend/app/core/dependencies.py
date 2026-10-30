"""FastAPI dependencies for DB, config, auth, and service access."""

import inspect
import ipaddress
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
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
MAX_TRACKED_IPS = 10_000  # cap on per-IP rate-limit state (memory bound)
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The only protected routes the setup wizard calls before an API key exists.
# Everything else stays closed until setup completes: otherwise anyone who can
# reach a fresh instance could add storage targets or run restores.
_SETUP_ROUTES = frozenset({("POST", "/api/discover/scan"), ("POST", "/api/directories/scan")})


# ---------------------------------------------------------------------------
# Database connections
# ---------------------------------------------------------------------------


async def _configure_connection(db: aiosqlite.Connection) -> None:
    db.row_factory = aiosqlite.Row
    # Per-connection pragmas: foreign keys are OFF by default in SQLite, which
    # silently disables every ON DELETE CASCADE in the schema.
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")


@asynccontextmanager
async def connect_db(db_path: Any = None):
    """Open an aiosqlite connection with Arkive's per-connection settings.

    Use this instead of bare ``aiosqlite.connect`` so foreign-key cascades and
    the busy timeout apply consistently.
    """
    async with aiosqlite.connect(db_path or _config.db_path) as db:
        await _configure_connection(db)
        yield db


async def get_db():
    """Yield async SQLite connection (foreign keys enforced)."""
    async with connect_db(_config.db_path) as db:
        yield db


def get_config() -> ArkiveConfig:
    """Return ArkiveConfig singleton."""
    return _config


# ---------------------------------------------------------------------------
# Client identity / trusted proxies
# ---------------------------------------------------------------------------


def _trusted_proxies() -> list:
    """Parse ARKIVE_TRUSTED_PROXIES (comma-separated IPs/CIDRs)."""
    raw = os.environ.get("ARKIVE_TRUSTED_PROXIES", "")
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid ARKIVE_TRUSTED_PROXIES entry: %r", item)
    return networks


def _is_trusted_proxy(host: str | None, networks: list | None = None) -> bool:
    if not host:
        return False
    networks = _trusted_proxies() if networks is None else networks
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting.

    Uses the TCP peer unless it is a configured trusted proxy, in which case the
    right-most untrusted ``X-Forwarded-For`` entry is used.
    """
    peer = request.client.host if request.client else "unknown"
    networks = _trusted_proxies()
    if not networks or not _is_trusted_proxy(peer, networks):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted_proxy(hop, networks):
            return hop
    return peer


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _prune_rate_limit_state(now: float) -> None:
    """Drop expired entries; evict oldest if still over the cap. Caller holds the lock."""
    for ip in [ip for ip, until in _lockouts.items() if until <= now]:
        _lockouts.pop(ip, None)
    for ip in list(_failed_attempts):
        recent = [t for t in _failed_attempts[ip] if now - t < RATE_LIMIT_WINDOW]
        if recent:
            _failed_attempts[ip] = recent
        else:
            _failed_attempts.pop(ip, None)
    overflow = len(_failed_attempts) - MAX_TRACKED_IPS
    if overflow > 0:
        oldest = sorted(_failed_attempts, key=lambda ip: _failed_attempts[ip][-1])[:overflow]
        for ip in oldest:
            _failed_attempts.pop(ip, None)
    overflow = len(_lockouts) - MAX_TRACKED_IPS
    if overflow > 0:
        for ip in sorted(_lockouts, key=_lockouts.get)[:overflow]:
            _lockouts.pop(ip, None)


def _track_failed_attempt(ip: str) -> None:
    """Track failed auth attempt for rate limiting."""
    with _rate_limit_lock:
        now = time.time()
        if ip not in _failed_attempts and len(_failed_attempts) >= MAX_TRACKED_IPS:
            _prune_rate_limit_state(now)
        _failed_attempts[ip] = [t for t in _failed_attempts.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
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


def reject_failed_credentials(ip: str, message: str = "Invalid API key") -> None:
    """Failure path for bad credentials: 429 while locked out, else track and 401.

    Lockout is only consulted here, so a valid API key or browser session from
    a shared IP (NAT / reverse proxy) is never blocked by someone else's
    failed attempts.
    """
    if _is_locked_out(ip):
        raise RateLimitError("Too many failed attempts. Try again later.", retry_after=LOCKOUT_DURATION)
    _track_failed_attempt(ip)
    raise AuthError(message)


# ---------------------------------------------------------------------------
# Origin (CSRF) checks for cookie-authenticated writes
# ---------------------------------------------------------------------------


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


def _normalize_host(netloc: str | None) -> str | None:
    """Lower-case host[:port] with default ports (80/443) removed."""
    if not netloc:
        return None
    host = netloc.strip().lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    for default_port in (":80", ":443"):
        if host.endswith(default_port):
            host = host[: -len(default_port)]
            break
    return host or None


def _origin_host(origin: str | None) -> str | None:
    if not origin or origin == "null":
        return None
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        return None
    return _normalize_host(parsed.netloc)


def _enforce_session_origin(request: Request) -> None:
    """Block cross-site cookie-authenticated writes.

    Compares the Origin/Referer host[:port] with the request's Host header
    (scheme ignored, so a TLS-terminating reverse proxy that talks plain HTTP to
    Arkive still matches), plus any explicitly configured CORS origins. When the
    TCP peer is a configured trusted proxy, ``X-Forwarded-Host`` is accepted too.
    """
    if request.method.upper() not in _UNSAFE_METHODS:
        return

    origin = _request_origin(request)
    origin_host = _origin_host(origin)
    if origin_host is None:
        raise HTTPException(status_code=403, detail="Cross-site cookie-authenticated request blocked")

    allowed_hosts = {
        _normalize_host(request.headers.get("host")),
        _normalize_host(urlsplit(str(request.base_url)).netloc),
    }
    for configured in _config.cors_origins:
        allowed_hosts.add(_origin_host(configured.rstrip("/")))
    peer = request.client.host if request.client else None
    if _is_trusted_proxy(peer):
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0]
        allowed_hosts.add(_normalize_host(forwarded_host))
    allowed_hosts.discard(None)

    if origin_host not in allowed_hosts:
        raise HTTPException(status_code=403, detail="Cross-site cookie-authenticated request blocked")


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _is_missing_table_error(exc: Exception) -> bool:
    return "no such table" in str(exc).lower()


async def _load_api_key_hash(db: aiosqlite.Connection) -> tuple[bool, str | None]:
    """Return (bootstrapping, api_key_hash).

    Only a missing schema counts as first-boot bootstrap. Any other database
    error (locked, I/O error, ...) fails closed with 503 instead of being
    treated as "setup mode", which would grant unauthenticated access.
    """
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
        if _is_missing_table_error(exc):
            return True, None
        logger.error("Auth check could not read settings: %s", exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable") from exc
    if not row:
        return True, None
    return False, row["value"] if isinstance(row, aiosqlite.Row) else row[0]


def request_has_valid_credentials(request: Request, api_key_hash: str | None) -> bool:
    """Non-raising credential check (API key header or browser session cookie)."""
    if not api_key_hash:
        return False
    api_key = request.headers.get("X-API-Key")
    if api_key and verify_api_key(api_key, api_key_hash):
        return True
    session_token = request.cookies.get(BROWSER_SESSION_COOKIE)
    return bool(session_token and verify_browser_session(session_token, api_key_hash))


async def require_auth(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    """Validate API key from X-API-Key header or the browser session cookie."""
    ip = client_ip(request)

    bootstrapping, api_key_hash = await _load_api_key_hash(db)
    if bootstrapping:
        if (request.method, request.url.path) in _SETUP_ROUTES:
            return
        raise AuthError("Setup has not been completed")

    api_key = request.headers.get("X-API-Key")
    if api_key:
        if not verify_api_key(api_key, api_key_hash):
            reject_failed_credentials(ip, "Invalid API key")
        clear_rate_limit(ip)
        return

    session_token = request.cookies.get(BROWSER_SESSION_COOKIE)
    if session_token and verify_browser_session(session_token, api_key_hash):
        _enforce_session_origin(request)
        return

    if _is_locked_out(ip):
        raise RateLimitError("Too many failed attempts. Try again later.", retry_after=LOCKOUT_DURATION)
    raise AuthError("Missing API key (X-API-Key header required)")


async def require_sse_auth(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> None:
    """Validate short-lived SSE token from ?token= query param."""
    bootstrapping, _hash = await _load_api_key_hash(db)
    if bootstrapping:
        raise AuthError("Setup has not been completed")

    token = request.query_params.get("token")
    if not token:
        raise AuthError("Missing SSE token (?token= required)")
    if not verify_sse_token(token):
        raise AuthError("Invalid or expired SSE token")


# Service accessors
def get_orchestrator(request: Request) -> Any:
    return request.app.state.orchestrator


class _EventBusAdapter:
    """Normalizes sync/async ``publish`` implementations behind one async API."""

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


def get_event_bus(request: Request) -> Any:
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        return None
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
    return getattr(request.app.state, "cloud_manager", None)


def get_notifier(request: Request) -> Any:
    return request.app.state.notifier


def get_restore_plan(request: Request) -> Any:
    return request.app.state.restore_plan


def get_verify_engine(request: Request) -> Any:
    return getattr(request.app.state, "verify_engine", None)
