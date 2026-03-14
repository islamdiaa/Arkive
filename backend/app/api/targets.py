"""
Storage Targets API endpoints.
CRUD operations for backup storage destinations.
Includes test-connection, OAuth flows, and usage reporting.
"""

import json
import logging
import os
import re
import secrets
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import ArkiveConfig
from app.core.dependencies import (
    get_cloud_manager,
    get_config,
    get_db,
    get_event_bus,
    require_auth,
)
from app.core.security import decrypt_config, encrypt_config
from app.models.targets import TargetCreate, TargetUpdate

logger = logging.getLogger("arkive.api.targets")
router = APIRouter(prefix="/targets", tags=["targets"], dependencies=[Depends(require_auth)])

# In-memory store for pending OAuth states with TTL cleanup
_oauth_pending: dict[str, dict] = {}
_OAUTH_MAX_PENDING = 50  # Maximum pending OAuth states
_OAUTH_STATE_TTL = 600  # 10 minutes TTL for pending states


def _cleanup_expired_oauth_states() -> None:
    """Remove expired OAuth states to prevent unbounded memory growth."""
    import time

    now = time.time()
    expired = [state for state, data in _oauth_pending.items() if now - data.get("_created_ts", 0) > _OAUTH_STATE_TTL]
    for state in expired:
        del _oauth_pending[state]


OAUTH_PROVIDERS = {
    "dropbox": {
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scopes": [],
        "response_type": "code",
        "token_access_type": "offline",
    },
    "gdrive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    },
}

VALID_TYPES = {"b2", "dropbox", "gdrive", "s3", "local", "sftp", "wasabi"}


def _validate_target_id(target_id: str) -> None:
    """Validate target ID to prevent path traversal and injection attacks."""
    if not target_id or not re.match(r"^[a-zA-Z0-9_-]{1,64}$", target_id):
        raise HTTPException(status_code=400, detail="Invalid target ID format")


def _sanitize_name(name: str) -> str:
    """Strip HTML tags from a target name."""
    return re.sub(r"<[^>]+>", "", name).strip()


def _normalize_config(config: dict | None) -> dict:
    """Normalize provider config before validation and persistence."""
    normalized: dict = {}
    for key, value in (config or {}).items():
        normalized[key] = value.strip() if isinstance(value, str) else value
    return normalized


def _validate_local_path(config: dict) -> list[str]:
    """Validate that a local target path is absolute and safe."""
    errors: list[str] = []
    path = config.get("path", "")
    if not path:
        return errors
    if not os.path.isabs(path):
        errors.append("Local path must be absolute (no relative/traversal paths)")
        return errors
    # Normalize to resolve any .. components
    normalized = os.path.normpath(path)
    if ".." in path:
        errors.append("Path traversal (..) is not allowed")
        return errors
    resolved = os.path.realpath(normalized)
    # Block sensitive system directories
    blocked_prefixes = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/proc", "/sys", "/dev", "/var/run", "/root")
    for prefix in blocked_prefixes:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            errors.append(f"Cannot use system directory as backup target: {prefix}")
        elif resolved == prefix or resolved.startswith(prefix + "/"):
            errors.append(f"Cannot use path resolving to system directory as backup target: {prefix}")
    config["path"] = normalized
    return errors


def _redact_config(config: dict) -> dict:
    """Return config dict with sensitive values masked."""
    redacted = {}
    for k, v in config.items():
        if any(s in k.lower() for s in ("key", "secret", "password", "token")):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def _validate_provider_config(provider: str, config: dict) -> list[str]:
    """Return a list of validation error messages for the given provider config."""
    errors: list[str] = []
    if provider == "b2":
        if not config.get("key_id"):
            errors.append("Application Key ID is required")
        if not config.get("app_key"):
            errors.append("Application Key is required")
        if not config.get("bucket"):
            errors.append("Bucket name is required")
    elif provider == "s3":
        if not config.get("endpoint"):
            errors.append("Endpoint URL is required")
        if not config.get("access_key"):
            errors.append("Access Key is required")
        if not config.get("secret_key"):
            errors.append("Secret Key is required")
        if not config.get("bucket"):
            errors.append("Bucket name is required")
    elif provider == "sftp":
        if not config.get("host"):
            errors.append("Host is required")
        if not config.get("username") and not config.get("user"):
            errors.append("Username is required")
    elif provider == "local":
        if not config.get("path"):
            errors.append("Path is required")
    elif provider == "dropbox":
        if not config.get("token"):
            errors.append("Access token is required")
    elif provider == "gdrive":
        if not config.get("client_id"):
            errors.append("Client ID is required")
    elif provider == "wasabi":
        if not config.get("access_key"):
            errors.append("Access Key is required")
        if not config.get("secret_key"):
            errors.append("Secret Key is required")
        if not config.get("bucket"):
            errors.append("Bucket name is required")
    return errors


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def list_targets(
    limit: int = 200,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
):
    """List all storage targets with redacted credentials."""
    cursor = await db.execute("SELECT COUNT(*) FROM storage_targets")
    total = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT * FROM storage_targets ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()

    targets = []
    for row in rows:
        target = dict(row)
        target["config"] = _redact_config(decrypt_config(target.get("config", "{}"), str(config.config_dir)))
        targets.append(target)

    return {
        "items": targets,
        "targets": targets,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.post("", status_code=201)
async def create_target(
    body: TargetCreate,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
    event_bus=Depends(get_event_bus),
):
    """Create a new storage target."""
    body.config = _normalize_config(body.config)
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid target type. Must be one of: {', '.join(VALID_TYPES)}",
        )

    errors = _validate_provider_config(body.type, body.config)
    if not body.name or not body.name.strip():
        errors.append("Target name is required")
    if body.type == "local":
        errors.extend(_validate_local_path(body.config))
    if body.name:
        body.name = _sanitize_name(body.name)
        if not body.name:
            errors.append("Target name cannot be empty after sanitization")
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"errors": errors, "message": "Validation failed"},
        )

    target_id = str(uuid.uuid4())[:8]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    encrypted = encrypt_config(body.config, str(config.config_dir))

    await db.execute(
        """INSERT INTO storage_targets
           (id, name, type, enabled, config, status, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, 'unknown', ?, ?)""",
        (target_id, body.name, body.type, encrypted, now, now),
    )

    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "target",
            "created",
            f"Storage target '{body.name}' created",
            json.dumps({"target_id": target_id, "target_name": body.name, "target_type": body.type}),
            "info",
            now,
        ),
    )
    await db.commit()

    if event_bus is not None:
        await event_bus.publish("target.created", {"target_id": target_id, "name": body.name})

    logger.info("Created storage target: %s (%s)", body.name, body.type)

    return {
        "id": target_id,
        "name": body.name,
        "type": body.type,
        "enabled": True,
        "config": _redact_config(body.config),
        "status": "unknown",
        "created_at": now,
        "updated_at": now,
    }


@router.get("/{target_id}")
async def get_target(
    target_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
):
    """Get a single storage target by ID."""
    _validate_target_id(target_id)
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Target not found")

    target = dict(row)
    target["config"] = _redact_config(decrypt_config(target.get("config", "{}"), str(config.config_dir)))
    return target


@router.put("/{target_id}")
async def update_target(
    target_id: str,
    body: TargetUpdate,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
    cloud_manager=Depends(get_cloud_manager),
    event_bus=Depends(get_event_bus),
):
    """Update a storage target."""
    _validate_target_id(target_id)
    if body.config is not None:
        body.config = _normalize_config(body.config)
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Target not found")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    updates: list[str] = []
    params: list = []
    errors: list[str] = []

    if body.name is not None:
        body.name = _sanitize_name(body.name)
        if not body.name:
            errors.append("Target name cannot be empty after sanitization")
        updates.append("name = ?")
        params.append(body.name)
    if body.config is not None:
        target_type = row["type"]
        if target_type == "local":
            errors.extend(_validate_local_path(body.config))
        errors.extend(_validate_provider_config(target_type, body.config))
        updates.append("config = ?")
        params.append(encrypt_config(body.config, str(config.config_dir)))

    if errors:
        raise HTTPException(
            status_code=422,
            detail={"errors": errors, "message": "Validation failed"},
        )
    if body.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if body.enabled else 0)

    updates.append("updated_at = ?")
    params.append(now)
    params.append(target_id)

    new_config = None
    if body.config is not None:
        new_config = body.config

    await db.execute(
        f"UPDATE storage_targets SET {', '.join(updates)} WHERE id = ?",
        params,  # nosec B608
    )

    changed = [u.split(" = ")[0] for u in updates if u != "updated_at = ?"]
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "target",
            "updated",
            f"Storage target '{row['name']}' updated",
            json.dumps({"target_id": target_id, "changed_fields": changed}),
            "info",
            now,
        ),
    )
    await db.commit()

    if new_config is not None and row["type"] != "local" and cloud_manager is not None:
        await cloud_manager.write_target_config(
            {
                "id": target_id,
                "type": row["type"],
                "config": new_config,
            }
        )

    if event_bus is not None:
        await event_bus.publish("target.updated", {"target_id": target_id, "changed_fields": changed})

    return await get_target(target_id, db, config)


@router.delete("/{target_id}")
async def delete_target(
    target_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    event_bus=Depends(get_event_bus),
):
    """Delete a storage target. Remote data is preserved."""
    _validate_target_id(target_id)
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Target not found")

    target_name = row["name"]

    await db.execute("DELETE FROM storage_targets WHERE id = ?", (target_id,))

    # Cascade: remove target from job target arrays
    cursor = await db.execute("SELECT id, targets FROM backup_jobs")
    for job_row in await cursor.fetchall():
        try:
            job_targets = (
                json.loads(job_row["targets"]) if isinstance(job_row["targets"], str) else (job_row["targets"] or [])
            )
        except (json.JSONDecodeError, TypeError):
            job_targets = []
        if target_id in job_targets:
            job_targets = [t for t in job_targets if t != target_id]
            await db.execute(
                "UPDATE backup_jobs SET targets = ? WHERE id = ?",
                (json.dumps(job_targets), job_row["id"]),
            )

    # Clean up related data
    await db.execute("DELETE FROM snapshots WHERE target_id = ?", (target_id,))
    await db.execute("DELETE FROM size_history WHERE target_id = ?", (target_id,))

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "target",
            "deleted",
            f"Storage target '{target_name}' deleted",
            json.dumps({"target_id": target_id, "target_name": target_name, "target_type": row["type"]}),
            "warning",
            now,
        ),
    )
    await db.commit()

    if event_bus is not None:
        await event_bus.publish("target.deleted", {"target_id": target_id, "name": target_name})

    logger.info("Deleted storage target: %s (%s)", target_id, target_name)
    return {"message": "Target deleted. Remote data has been preserved."}


# ---------------------------------------------------------------------------
# Test Connection
# ---------------------------------------------------------------------------


class TestConnectionRequest(BaseModel):
    type: str
    config: dict = {}


@router.post("/test-connection")
async def test_connection_inline(
    body: TestConnectionRequest,
    cloud_manager=Depends(get_cloud_manager),
):
    """Test a storage target connection without saving to DB."""
    if body.type not in VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid target type. Must be one of: {', '.join(VALID_TYPES)}")

    errors = _validate_provider_config(body.type, body.config)
    if errors:
        return {"success": False, "message": "; ".join(errors)}

    if body.type == "local":
        import os

        path = body.config.get("path", "")
        if os.path.isdir(path):
            return {"success": True, "message": f"Local path '{path}' is accessible"}
        return {"success": False, "message": f"Local path '{path}' does not exist or is not a directory"}

    # Cloud targets via cloud_manager
    temp_id = f"__test_{uuid.uuid4().hex[:8]}"
    temp_target = {"id": temp_id, "type": body.type, "config": body.config}
    try:
        if cloud_manager is not None:
            await cloud_manager.write_target_config(temp_target)
            result = await cloud_manager.test_target(temp_target)
            success = result.get("status") == "ok"
            return {"success": success, "message": result.get("message", "")}
        return {"success": False, "message": "Cloud manager not available"}
    except Exception as exc:
        logger.error("Test connection error for %s: %s", body.type, exc)
        # Don't leak internal error details to client
        return {"success": False, "message": "Connection test failed. Check server logs for details."}
    finally:
        if cloud_manager is not None:
            try:
                await cloud_manager.remove_target_config(temp_id)
            except Exception:
                pass


@router.post("/{target_id}/test")
async def test_target(
    target_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
    cloud_manager=Depends(get_cloud_manager),
):
    """Test connection to an existing storage target."""
    _validate_target_id(target_id)
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Target not found")

    target = dict(row)
    target_config = decrypt_config(target.get("config", "{}"), str(config.config_dir))
    target_type = target.get("type", "unknown")
    target_name = target.get("name", target_id)

    success = False
    message = ""

    try:
        if target_type == "local":
            import os

            path = target_config.get("path", "")
            if not path:
                message = "No path configured for local target"
            elif not os.path.isdir(path):
                message = f"Local path '{path}' does not exist or is not a directory"
            else:
                test_file = os.path.join(path, ".arkive_test_write")
                try:
                    with open(test_file, "w") as f:
                        f.write("test")
                    os.remove(test_file)
                    success = True
                    message = f"Local path '{path}' is accessible and writable"
                except OSError as exc:
                    message = f"Local path '{path}' is not writable: {exc}"

        elif target_type in ("b2", "s3", "sftp", "dropbox", "gdrive", "wasabi"):
            if cloud_manager is not None:
                test_target_dict = {"id": target_id, "type": target_type, "config": target_config}
                try:
                    await cloud_manager.write_target_config(test_target_dict)
                except Exception as exc:
                    message = f"Failed to configure remote for {target_type}: {exc}"
                else:
                    result = await cloud_manager.test_target(test_target_dict)
                    success = result.get("status") == "ok"
                    message = result.get("message", "")
            else:
                message = "Cloud manager not available"
        else:
            message = f"Unknown target type: {target_type}"

    except Exception as exc:
        message = f"Connection test failed: {exc}"
        logger.error("Target test error for %s: %s", target_id, exc)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_status = "ok" if success else "error"
    await db.execute(
        "UPDATE storage_targets SET last_tested = ?, status = ?, updated_at = ? WHERE id = ?",
        (now, new_status, now, target_id),
    )
    await db.commit()

    logger.info(
        "Target test for '%s' (%s): %s - %s", target_name, target_type, "success" if success else "failed", message
    )

    return {"success": success, "message": message, "tested_at": now}


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@router.get("/{target_id}/usage")
async def get_target_usage(
    target_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
    cloud_manager=Depends(get_cloud_manager),
):
    """Get storage usage for a target."""
    _validate_target_id(target_id)
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Target not found")

    target = dict(row)
    target_config = decrypt_config(target.get("config", "{}"), str(config.config_dir))
    target_type = target.get("type", "unknown")

    if target_type == "local":
        import shutil

        path = target_config.get("path", "/backups")
        try:
            usage = shutil.disk_usage(path)
            return {
                "target_id": target_id,
                "type": target_type,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to get disk usage: {exc}")

    if cloud_manager is None:
        raise HTTPException(status_code=500, detail="Cloud manager not available")

    usage_target = {"id": target_id, "type": target_type, "config": target_config}
    try:
        await cloud_manager.write_target_config(usage_target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to configure remote: {exc}")

    # Get usage via rclone about command
    env = os.environ.copy()
    env["RCLONE_CONFIG"] = str(cloud_manager.rclone_config)
    from app.utils.subprocess_runner import run_command

    about_result = await run_command(
        ["rclone", "about", f"{target_id}:", "--json"],
        env=env,
        timeout=30,
    )
    if about_result.returncode != 0:
        usage_data = {"error": about_result.stderr[:200]}
    else:
        try:
            usage_data = json.loads(about_result.stdout)
        except (json.JSONDecodeError, ValueError):
            usage_data = {"error": "Failed to parse rclone output"}

    result = {
        "target_id": target_id,
        "type": target_type,
        "total": usage_data.get("total"),
        "used": usage_data.get("used"),
        "free": usage_data.get("free"),
    }

    if usage_data.get("error"):
        result["error"] = usage_data["error"]

    return result


# ---------------------------------------------------------------------------
# OAuth Flows
# ---------------------------------------------------------------------------


class OAuthStartRequest(BaseModel):
    provider: str
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None


class OAuthCompleteRequest(BaseModel):
    provider: str
    code: str
    state: str
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    name: str | None = None


@router.post("/oauth/start")
async def oauth_start(
    body: OAuthStartRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Start an OAuth authorization flow for Dropbox or Google Drive."""
    provider = body.provider.lower()
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported OAuth provider. Must be one of: {', '.join(OAUTH_PROVIDERS.keys())}",
        )

    provider_config = OAUTH_PROVIDERS[provider]

    client_id = body.client_id
    if not client_id:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (f"oauth_{provider}_client_id",))
        row = await cursor.fetchone()
        if row:
            client_id = row[0]

    if not client_id:
        raise HTTPException(
            status_code=400,
            detail=f"client_id is required for {provider} OAuth.",
        )

    state = secrets.token_urlsafe(32)
    redirect_uri = body.redirect_uri or "http://localhost:8200/oauth/callback"

    # Cleanup expired states and enforce max pending limit
    _cleanup_expired_oauth_states()
    if len(_oauth_pending) >= _OAUTH_MAX_PENDING:
        raise HTTPException(
            status_code=429,
            detail="Too many pending OAuth flows. Try again later.",
        )

    import time as _time

    _oauth_pending[state] = {
        "provider": provider,
        "client_id": client_id,
        "client_secret": body.client_secret,
        "redirect_uri": redirect_uri,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_created_ts": _time.time(),
    }

    params = {
        "client_id": client_id,
        "response_type": provider_config["response_type"],
        "redirect_uri": redirect_uri,
        "state": state,
    }

    if provider == "dropbox":
        params["token_access_type"] = provider_config.get("token_access_type", "offline")
    elif provider == "gdrive":
        params["scope"] = " ".join(provider_config.get("scopes", []))
        params["access_type"] = provider_config.get("access_type", "offline")
        params["prompt"] = provider_config.get("prompt", "consent")

    authorization_url = f"{provider_config['auth_url']}?{urlencode(params)}"

    logger.info("OAuth flow started for %s (state=%s...)", provider, state[:8])

    return {
        "authorization_url": authorization_url,
        "state": state,
        "provider": provider,
        "redirect_uri": redirect_uri,
    }


@router.post("/oauth/complete")
async def oauth_complete(
    body: OAuthCompleteRequest,
    db: aiosqlite.Connection = Depends(get_db),
    config: ArkiveConfig = Depends(get_config),
    event_bus=Depends(get_event_bus),
):
    """Complete an OAuth flow by exchanging authorization code for tokens."""
    provider = body.provider.lower()
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported OAuth provider.")

    pending = _oauth_pending.get(body.state)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state token.")

    if pending["provider"] != provider:
        raise HTTPException(
            status_code=400,
            detail=f"OAuth state was initiated for '{pending['provider']}', not '{provider}'.",
        )

    del _oauth_pending[body.state]

    client_id = body.client_id or pending.get("client_id")
    client_secret = body.client_secret or pending.get("client_secret")
    redirect_uri = body.redirect_uri or pending.get("redirect_uri", "http://localhost:8200/oauth/callback")
    provider_config = OAUTH_PROVIDERS[provider]

    import httpx

    token_data = {
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                provider_config["token_url"],
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Token exchange failed: {provider} returned HTTP {resp.status_code}",
            )
        tokens = resp.json()
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Token exchange failed: could not reach {provider} ({exc})",
        )

    # Store full token JSON for rclone (it expects the complete token object)
    token_json = json.dumps(tokens)
    target_config_dict = {
        "token": token_json,
        "oauth_provider": provider,
    }

    if provider == "gdrive":
        target_config_dict["client_id"] = client_id
        if client_secret:
            target_config_dict["client_secret"] = client_secret

    target_id = str(uuid.uuid4())[:8]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    target_name = body.name or f"{provider.capitalize()} ({target_id})"
    encrypted = encrypt_config(target_config_dict, str(config.config_dir))

    await db.execute(
        """INSERT INTO storage_targets
           (id, name, type, enabled, config, status, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, 'ok', ?, ?)""",
        (target_id, target_name, provider, encrypted, now, now),
    )
    await db.commit()

    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "target",
            "created",
            f"Storage target '{target_name}' created via OAuth",
            json.dumps({"target_id": target_id, "target_type": provider, "method": "oauth"}),
            "info",
            now,
        ),
    )
    await db.commit()

    if event_bus is not None:
        await event_bus.publish("target.created", {"target_id": target_id, "name": target_name, "method": "oauth"})

    logger.info("OAuth target created: %s (%s) id=%s", target_name, provider, target_id)

    needs_reauth = bool(tokens.get("needs_reauth"))
    return {
        "id": target_id,
        "name": target_name,
        "type": provider,
        "enabled": True,
        "status": "needs_reauth" if needs_reauth else "ok",
        "created_at": now,
        "updated_at": now,
        "oauth_complete": True,
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "needs_reauth": needs_reauth,
    }
