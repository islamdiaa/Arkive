"""Authentication and setup API routes."""

import json
import logging
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.dependencies import (
    _enforce_session_origin,
    _is_locked_out,
    _track_failed_attempt,
    clear_rate_limit,
    get_db,
    get_scheduler,
    require_auth,
)
from app.core.platform import Platform
from app.core.security import (
    BROWSER_SESSION_COOKIE,
    BROWSER_SESSION_TTL,
    encrypt_config,
    encrypt_value,
    generate_api_key,
    generate_browser_session,
    generate_sse_token,
    hash_api_key,
    verify_api_key,
    verify_browser_session,
)
from app.models.settings import SetupCompleteRequest

logger = logging.getLogger("arkive.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-memory rate limiter for the setup endpoint (unauthenticated surface)
_setup_attempts: dict[str, list[float]] = defaultdict(list)
SETUP_RATE_LIMIT = 5  # max attempts per window
SETUP_RATE_WINDOW = 900  # 15 minutes in seconds
_MAX_TRACKED_IPS = 10_000  # cap to prevent memory exhaustion from distributed attacks


class LoginRequest(BaseModel):
    api_key: str = ""


def _reset_setup_rate_limit() -> None:
    """Clear all setup rate-limit state. Used by test fixtures between app instances."""
    _setup_attempts.clear()


def _check_setup_rate_limit(request: Request) -> None:
    """Rate-limit setup attempts per IP. Raises 429 when exceeded."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - SETUP_RATE_WINDOW

    # Prune timestamps outside the current window
    _setup_attempts[client_ip] = [ts for ts in _setup_attempts[client_ip] if ts > window_start]

    # Remove empty entries to prevent unbounded dict growth
    if not _setup_attempts[client_ip]:
        del _setup_attempts[client_ip]
    elif len(_setup_attempts[client_ip]) >= SETUP_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many setup attempts. Try again later.")

    # Evict oldest entries if we exceed the IP cap (DoS protection)
    if len(_setup_attempts) >= _MAX_TRACKED_IPS:
        oldest_ip = min(_setup_attempts, key=lambda ip: _setup_attempts[ip][0])
        del _setup_attempts[oldest_ip]

    _setup_attempts[client_ip].append(now)


def _default_setup_directories(request: Request) -> list[tuple[str, str]]:
    """Return first-boot default watched directories for the current platform."""
    platform = getattr(getattr(request.app, "state", None), "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "")
    if platform_value != Platform.UNRAID.value:
        return []

    defaults: list[tuple[str, str]] = []
    appdata_path = Path("/mnt/user/appdata")
    if appdata_path.is_dir():
        defaults.append((str(appdata_path), "Appdata"))
    return defaults


def _set_browser_session(response: Response, request: Request, api_key_hash: str) -> None:
    response.set_cookie(
        BROWSER_SESSION_COOKIE,
        generate_browser_session(api_key_hash),
        max_age=BROWSER_SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


def _clear_browser_session(response: Response, request: Request) -> None:
    response.delete_cookie(
        BROWSER_SESSION_COOKIE,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


async def _session_payload(request: Request, db: aiosqlite.Connection) -> dict[str, object]:
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return {"setup_required": True, "authenticated": False}
    if not row:
        return {"setup_required": True, "authenticated": False}

    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'setup_completed_at'")
        ts_row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        ts_row = None
    session_token = request.cookies.get(BROWSER_SESSION_COOKIE, "")
    payload: dict[str, object] = {
        "setup_required": False,
        "authenticated": verify_browser_session(session_token, row[0]),
    }
    if ts_row:
        payload["setup_completed_at"] = ts_row[0]
    return payload


@router.post("/setup", status_code=201)
async def complete_setup(
    request: Request,
    response: Response,
    body: SetupCompleteRequest,
    db: aiosqlite.Connection = Depends(get_db),
    scheduler=Depends(get_scheduler),
):
    """Complete initial setup — generates API key, stores encryption password, creates default jobs."""
    # Rate-limit FIRST — before any other check so attackers can't probe indefinitely
    _check_setup_rate_limit(request)

    # Check if already set up
    cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
    if await cursor.fetchone():
        raise HTTPException(409, "Setup already completed")
    if not body.encryption_password:
        raise HTTPException(422, "encryption_password is required")

    # Generate API key
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Extract web_url from request (M4) — auto-detect from request base_url
    web_url = str(request.base_url).rstrip("/")

    # Store settings
    settings = [
        ("api_key_hash", api_key_hash, 0),
        ("api_key", encrypt_value(api_key), 1),
        ("encryption_password", encrypt_value(body.encryption_password), 1),
        ("setup_completed", "true", 0),
        ("setup_completed_at", now, 0),
        ("web_url", web_url, 0),
    ]
    for key, value, encrypted in settings:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
            (key, value, encrypted, now),
        )

    # Create default backup jobs
    target_ids = list(body.target_ids or [])
    directory_ids = list(body.directory_ids or [])
    schedules = body.schedules or {}

    if not target_ids and body.storage:
        from app.api.targets import (
            VALID_TYPES,
            _sanitize_name,
            _validate_local_path,
            _validate_provider_config,
        )

        storage = dict(body.storage)
        storage_type = str(storage.get("type", "")).strip()
        storage_name = _sanitize_name(str(storage.get("name") or f"{storage_type.title()} Target"))
        storage_config = {k: v for k, v in storage.items() if k not in {"type", "name"}}

        if storage_type in VALID_TYPES and storage_name:
            errors = _validate_provider_config(storage_type, storage_config)
            if storage_type == "local":
                errors.extend(_validate_local_path(storage_config))
            if not errors:
                target_id = str(uuid.uuid4())[:8]
                encrypted_config = encrypt_config(
                    storage_config, str(Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")))
                )
                await db.execute(
                    """INSERT INTO storage_targets
                       (id, name, type, enabled, config, status, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, 'unknown', ?, ?)""",
                    (target_id, storage_name, storage_type, encrypted_config, now, now),
                )
                target_ids.append(target_id)

    candidate_directories = list(body.directories or [])
    if not directory_ids and not candidate_directories:
        candidate_directories = [path for path, _label in _default_setup_directories(request)]

    if not directory_ids and candidate_directories:
        default_labels = {path: label for path, label in _default_setup_directories(request)}
        for index, directory in enumerate(candidate_directories, start=1):
            path = Path(directory)
            if not path.is_absolute():
                continue
            label = default_labels.get(str(path)) or path.name or f"Directory {index}"
            dir_id = str(uuid.uuid4())[:8]
            await db.execute(
                """INSERT INTO watched_directories
                   (id, path, label, exclude_patterns, enabled, created_at)
                   VALUES (?, ?, ?, '[]', 1, ?)""",
                (dir_id, str(path), label, now),
            )
            directory_ids.append(dir_id)

    jobs = [
        (str(uuid.uuid4())[:8], "DB Dumps", "db_dump", schedules.get("db_dump", body.db_dump_schedule)),
        (str(uuid.uuid4())[:8], "Cloud Sync", "full", schedules.get("cloud_sync", body.cloud_sync_schedule)),
        (str(uuid.uuid4())[:8], "Flash Backup", "flash", schedules.get("flash", body.flash_schedule)),
    ]
    jobs_created = []
    for job_id, name, job_type, schedule in jobs:
        # Cloud Sync gets target_ids, all jobs get directory_ids
        job_targets = target_ids if job_type == "full" else []
        job_dirs = directory_ids if directory_ids else candidate_directories
        await db.execute(
            """INSERT INTO backup_jobs (id, name, type, schedule, targets, directories)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, name, job_type, schedule, json.dumps(job_targets), json.dumps(job_dirs)),
        )
        jobs_created.append(
            {
                "id": job_id,
                "name": name,
                "type": job_type,
                "schedule": schedule,
                "targets": job_targets,
                "directories": list(job_dirs),
            }
        )

    await db.commit()

    # Clear rate limit state for this IP to prevent post-setup lockout
    if request.client:
        clear_rate_limit(request.client.host)

    # Save API key to file for CLI access (mode 0o600 — owner read/write only)
    api_key_path = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / ".api_key"
    fd = os.open(str(api_key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, api_key.encode())
    finally:
        os.close(fd)

    # Register jobs with scheduler
    if scheduler is not None:
        for job_id, _name, _job_type, schedule in jobs:
            if hasattr(scheduler, "reschedule_job"):
                await scheduler.reschedule_job(job_id, schedule)
            elif hasattr(scheduler, "add_job"):
                await scheduler.add_job({"id": job_id, "schedule": schedule, "enabled": True})

    # Trigger first backup if requested (M5)
    first_backup_triggered = False
    if body.run_first_backup and scheduler is not None:
        try:
            # Find the cloud sync job (type="full") to run first
            full_job = next((j for j in jobs_created if j["type"] == "full"), None)
            if full_job and hasattr(scheduler, "trigger_job"):
                await scheduler.trigger_job(full_job["id"])
                first_backup_triggered = True
        except Exception:
            logger.warning("Failed to trigger first backup — will run on next schedule", exc_info=True)

    _set_browser_session(response, request, api_key_hash)

    return {
        "api_key": api_key,
        "message": "Setup complete. Save this API key — it will not be shown again.",
        "first_backup_triggered": first_backup_triggered,
        "jobs_created": len(jobs_created),
        "setup_completed_at": now,
        "web_url": web_url,
    }


@router.get("/session")
async def get_session(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """Check setup status and return session info."""
    return await _session_payload(request, db)


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Create a browser session from a valid API key."""
    client_ip = request.client.host if request.client else "unknown"
    if _is_locked_out(client_ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="Setup required before login.")

    if not body.api_key or not verify_api_key(body.api_key, row[0]):
        _track_failed_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Invalid API key")

    clear_rate_limit(client_ip)
    _set_browser_session(response, request, row[0])
    payload = await _session_payload(request, db)
    payload["authenticated"] = True
    return payload


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Clear the browser session cookie."""
    if request.cookies.get(BROWSER_SESSION_COOKIE):
        _enforce_session_origin(request)
    _clear_browser_session(response, request)
    return {"authenticated": False, "message": "Logged out"}


@router.post("/rotate-key")
@router.post("/regenerate")
async def rotate_api_key(
    request: Request,
    response: Response,
    _auth=Depends(require_auth),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Generate a new API key (requires current auth)."""
    # This endpoint requires auth via the dependency
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    await db.execute(
        "UPDATE settings SET value = ?, updated_at = ? WHERE key = 'api_key_hash'",
        (api_key_hash, now),
    )
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 1, ?)",
        ("api_key", encrypt_value(api_key), now),
    )
    await db.commit()

    # Update .api_key file for CLI access (mode 0o600 — owner read/write only)
    api_key_path = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / ".api_key"
    fd = os.open(str(api_key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, api_key.encode())
    finally:
        os.close(fd)

    _set_browser_session(response, request, api_key_hash)

    return {
        "api_key": api_key,
        "message": "API key rotated. Save this key — it won't be shown again.",
    }


@router.post("/sse-token", status_code=200)
async def issue_sse_token(_auth=Depends(require_auth)):
    """Issue a short-lived (60s) single-use token for SSE connections."""
    try:
        token = generate_sse_token()
    except ValueError:
        raise HTTPException(status_code=429, detail="Too many outstanding SSE tokens")
    return {"token": token, "expires_in": 60}
