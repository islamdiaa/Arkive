"""
Settings API endpoints.
CRUD for application settings with secret redaction.
Includes config export/import as YAML and cron preview.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import aiosqlite
import yaml
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.core.dependencies import get_cloud_manager, get_config, get_db, get_event_bus, require_auth
from app.core.security import decrypt_config, decrypt_value, encrypt_config, encrypt_value, is_encrypted
from app.core.activity import log_activity
from app.services.freshness import ping_heartbeat
from app.services.recovery_kit import build_recovery_kit
from app.services.settings_store import DEFAULTS, get_setting, set_setting

logger = logging.getLogger("arkive.api.settings")
router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_auth)])

# Bandwidth limit: positive integer in KiB/s (restic --limit-upload expects an integer),
# or empty string to disable. "0" is rejected (restic treats 0 as unlimited, misleading).
# Suffixes like "50M" / "100K" are NOT accepted — restic wants raw KiB/s integers.
_BANDWIDTH_RE = re.compile(r"^([1-9]\d*)?$")

# Sensitive key detection
SENSITIVE_KEYS = {"api_key_hash", "restic_password", "encryption_password"}
SENSITIVE_PATTERNS = ["key_hash", "password", "secret", "token", "credential"]
REDACTED = "********"
EXPORT_EXCLUDED_KEYS = {"api_key_hash"}


def _is_sensitive(key: str, encrypted: int) -> bool:
    """Check if a setting key contains sensitive data."""
    if key in SENSITIVE_KEYS:
        return True
    if encrypted:
        return True
    return any(pattern in key.lower() for pattern in SENSITIVE_PATTERNS)


def _int_setting(values: dict, key: str, default: int) -> int:
    """Parse an integer setting, falling back to the default on bad stored data."""
    try:
        return int(values.get(key) or default)
    except (TypeError, ValueError):
        return default


_RETENTION_KEYS = {"retention_days", "keep_daily", "keep_weekly", "keep_monthly", "flash_retention"}
_DISK_SPACE_KEYS = {"min_disk_space_bytes", "warn_disk_space_bytes"}
# key -> (minimum, maximum) for bounded integer settings
_BOUNDED_INT_KEYS = {
    "stale_backup_hours": (0, 8760),
    "restore_drill_max_databases": (1, 100),
    "restore_drill_max_dump_mb": (1, 1024 * 1024),
}
_BOOL_KEYS = {"restore_drill_enabled"}
_CHOICE_KEYS = {"dump_compression": {"none", "gzip"}}
_HTTP_URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$")


def _validate_setting_value(key: str, value) -> str:
    """Validate one setting value; return it as the string to store.

    Raises HTTPException(422) on invalid input. Shared by single/bulk update and
    config import so all write paths apply the same rules.
    """
    if value is None:
        value = ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    if not isinstance(value, (str, int, float)):
        raise HTTPException(status_code=422, detail=f"Invalid value for '{key}': must be a string or number")
    text = str(value)
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
        raise HTTPException(status_code=422, detail=f"Invalid value for '{key}': control characters are not allowed")

    if key == "timezone":
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(text)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Invalid timezone: '{text}'")
    if key in _RETENTION_KEYS:
        try:
            int_val = int(text)
        except (ValueError, TypeError):
            int_val = 0
        if int_val < 1:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid value for '{key}': must be a positive integer (got {text})",
            )
    if key in _DISK_SPACE_KEYS:
        try:
            int_val = int(text)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"'{key}' must be an integer")
        if int_val < 0:
            raise HTTPException(status_code=422, detail=f"'{key}' must be non-negative")
    if key in _BOUNDED_INT_KEYS:
        low, high = _BOUNDED_INT_KEYS[key]
        try:
            int_val = int(text)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"'{key}' must be an integer")
        if not low <= int_val <= high:
            raise HTTPException(status_code=422, detail=f"'{key}' must be between {low} and {high}")
        text = str(int_val)
    if key in _BOOL_KEYS:
        lowered = text.strip().lower()
        if lowered not in ("true", "false"):
            raise HTTPException(status_code=422, detail=f"'{key}' must be true or false")
        text = lowered
    if key in _CHOICE_KEYS and text not in _CHOICE_KEYS[key]:
        raise HTTPException(
            status_code=422,
            detail=f"'{key}' must be one of: {', '.join(sorted(_CHOICE_KEYS[key]))}",
        )
    if key == "heartbeat_url":
        text = text.strip()
        if text and (len(text) > 2048 or not _HTTP_URL_RE.match(text)):
            raise HTTPException(status_code=422, detail="heartbeat_url must be an http(s) URL or empty")
    if key == "restore_drill_schedule":
        text = text.strip()
        if len(text.split()) != 5 or not croniter.is_valid(text):
            raise HTTPException(status_code=422, detail=f"Invalid cron expression for '{key}': '{text}'")
    if key == "bandwidth_limit" and text and not _BANDWIDTH_RE.match(text):
        raise HTTPException(
            status_code=422,
            detail="bandwidth_limit must be a positive integer (KiB/s) or empty to disable",
        )
    return text


async def _reload_drill_job(request: Request) -> None:
    """Re-register the restore drill system job after its settings changed."""
    scheduler = getattr(request.app.state, "scheduler", None)
    reload = getattr(scheduler, "reload_restore_drill_job", None)
    if reload is None:
        return
    try:
        await reload()
    except Exception as exc:
        logger.warning("Could not reschedule restore drills: %s", exc)


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def get_settings(db: aiosqlite.Connection = Depends(get_db)):
    """Get all application settings with sensitive values redacted."""
    cursor = await db.execute(
        "SELECT key, value, encrypted, updated_at FROM settings ORDER BY key"
    )
    rows = await cursor.fetchall()

    items = []
    values: dict[str, str] = {}
    for row in rows:
        key = row["key"]
        sensitive = _is_sensitive(key, row["encrypted"])
        values[key] = row["value"]
        items.append({
            "key": key,
            "value": REDACTED if sensitive else row["value"],
            "encrypted": bool(row["encrypted"]),
            "sensitive": sensitive,
            "has_value": bool(row["value"]),
            "updated_at": row["updated_at"],
        })

    keep_daily = _int_setting(values, "keep_daily", 7)
    keep_weekly = _int_setting(values, "keep_weekly", 4)
    keep_monthly = _int_setting(values, "keep_monthly", 6)
    return {
        "items": items,
        "total": len(items),
        "server_name": values.get("server_name", ""),
        "timezone": values.get("timezone", "UTC"),
        "retention_days": _int_setting(values, "retention_days", 30),
        "keep_daily": keep_daily,
        "keep_weekly": keep_weekly,
        "keep_monthly": keep_monthly,
        "log_level": values.get("log_level", "INFO"),
        "web_url": values.get("web_url", ""),
        "theme": values.get("theme", "dark"),
        "bandwidth_limit": values.get("bandwidth_limit", ""),
        "heartbeat_url": values.get("heartbeat_url", DEFAULTS["heartbeat_url"]),
        "stale_backup_hours": _int_setting(values, "stale_backup_hours", 0),
        "dump_compression": values.get("dump_compression", DEFAULTS["dump_compression"]),
        "restore_drill_enabled": values.get(
            "restore_drill_enabled", DEFAULTS["restore_drill_enabled"]) == "true",
        "restore_drill_schedule": values.get("restore_drill_schedule", DEFAULTS["restore_drill_schedule"]),
        "restore_drill_max_databases": _int_setting(
            values, "restore_drill_max_databases", int(DEFAULTS["restore_drill_max_databases"])),
        "restore_drill_max_dump_mb": _int_setting(
            values, "restore_drill_max_dump_mb", int(DEFAULTS["restore_drill_max_dump_mb"])),
        "recovery_kit_confirmed_at": values.get("recovery_kit_confirmed_at"),
        "api_key_set": "api_key_hash" in values,
        "encryption_password_set": ("encryption_password" in values or "restic_password" in values),
    }


class SettingUpdate(BaseModel):
    value: str


class BulkSettingsUpdate(BaseModel):
    settings: dict[str, str]


class ResetConfirm(BaseModel):
    confirm: bool = False


WRITABLE_SETTINGS = {
    "server_name", "timezone", "theme", "log_level", "web_url",
    "retention_days",
    "keep_daily", "keep_weekly", "keep_monthly", "flash_retention",
    "backup_schedule", "db_dump_schedule", "cloud_sync_schedule", "flash_schedule",
    "notify_on_success", "notify_on_failure",
    "min_disk_space_bytes", "warn_disk_space_bytes",
    "bandwidth_limit",
    "heartbeat_url", "stale_backup_hours", "dump_compression",
    "restore_drill_enabled", "restore_drill_schedule",
    "restore_drill_max_databases", "restore_drill_max_dump_mb",
}
# Settings whose change must re-register the restore drill system job.
_DRILL_SCHEDULE_KEYS = {"restore_drill_enabled", "restore_drill_schedule"}
READONLY_SETTINGS = {
    "api_key_hash", "api_key", "encryption_password", "restic_password",
    "setup_completed_at", "setup_completed", "platform",
}


@router.put("")
async def update_settings_bulk(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    event_bus=Depends(get_event_bus),
):
    """Update multiple settings at once."""
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid settings payload")
    settings_payload = payload["settings"] if isinstance(payload.get("settings"), dict) else payload

    # Reject writes to protected settings
    blocked = [k for k in settings_payload if k in READONLY_SETTINGS]
    if blocked:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot modify protected settings: {', '.join(blocked)}. Use dedicated endpoints.",
        )

    # Enforce writable allowlist
    unknown = [k for k in settings_payload if k not in WRITABLE_SETTINGS and k not in READONLY_SETTINGS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown settings: {', '.join(unknown)}. Allowed: {', '.join(sorted(WRITABLE_SETTINGS))}",
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = []

    cleaned = {key: _validate_setting_value(key, value) for key, value in settings_payload.items()}

    for key, value in cleaned.items():
        encrypted = 1 if _is_sensitive(key, 0) else 0

        cursor = await db.execute("SELECT key FROM settings WHERE key = ?", (key,))
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "UPDATE settings SET value = ?, encrypted = ?, updated_at = ? WHERE key = ?",
                (value, encrypted, now, key),
            )
        else:
            await db.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
                (key, value, encrypted, now),
            )

        updated.append({
            "key": key,
            "value": REDACTED if _is_sensitive(key, encrypted) else value,
            "encrypted": bool(encrypted),
            "sensitive": _is_sensitive(key, encrypted),
            "updated_at": now,
        })

    await db.commit()

    if _DRILL_SCHEDULE_KEYS & cleaned.keys():
        await _reload_drill_job(request)

    if event_bus is not None:
        await event_bus.publish("settings.updated", {"count": len(updated)})

    logger.info("Bulk settings update: %d settings updated", len(updated))
    return {"items": updated, "total": len(updated), "updated_at": now}


@router.put("/{key}")
async def update_setting(
    key: str,
    body: SettingUpdate,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Update a specific setting."""
    if key in READONLY_SETTINGS:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot modify '{key}' directly. Use the dedicated endpoint.",
        )
    if key not in WRITABLE_SETTINGS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown setting '{key}'. Allowed: {', '.join(sorted(WRITABLE_SETTINGS))}",
        )

    body.value = _validate_setting_value(key, body.value)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    encrypted = 1 if _is_sensitive(key, 0) else 0

    cursor = await db.execute("SELECT key FROM settings WHERE key = ?", (key,))
    existing = await cursor.fetchone()

    if existing:
        await db.execute(
            "UPDATE settings SET value = ?, encrypted = ?, updated_at = ? WHERE key = ?",
            (body.value, encrypted, now, key),
        )
    else:
        await db.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
            (key, body.value, encrypted, now),
        )

    await db.commit()
    logger.info("Setting '%s' updated", key)
    if key in _DRILL_SCHEDULE_KEYS:
        await _reload_drill_job(request)

    return {
        "key": key,
        "value": REDACTED if _is_sensitive(key, encrypted) else body.value,
        "encrypted": bool(encrypted),
        "sensitive": _is_sensitive(key, encrypted),
        "updated_at": now,
    }



@router.post("/recovery-kit")
async def generate_recovery_kit(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    config=Depends(get_config),
):
    """Everything needed to open the backups if this server is lost:
    encryption password, repository locations, restore steps."""
    try:
        kit = await build_recovery_kit(db, config, getattr(request.app.state, "docker_client", None))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await log_activity(db, "system", "recovery_kit", "Recovery kit generated", severity="info")
    return JSONResponse(kit, headers={"Cache-Control": "no-store"})


@router.post("/recovery-kit/confirm")
async def confirm_recovery_kit(db: aiosqlite.Connection = Depends(get_db)):
    """Record that the user stored the recovery kit outside this server."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await set_setting(db, "recovery_kit_confirmed_at", now)
    await log_activity(db, "system", "recovery_kit", "Recovery kit marked as saved")
    return {"confirmed_at": now}


class HeartbeatTest(BaseModel):
    url: str | None = None


@router.post("/heartbeat/test")
async def test_heartbeat(body: HeartbeatTest | None = None, db: aiosqlite.Connection = Depends(get_db)):
    """Ping the heartbeat URL now (the given one, or the saved setting)."""
    url = (body.url if body and body.url else await get_setting(db, "heartbeat_url")).strip()
    if not url:
        raise HTTPException(status_code=422, detail="No heartbeat URL configured")
    url = _validate_setting_value("heartbeat_url", url)
    return await ping_heartbeat(url)


@router.post("/reset")
async def reset_settings(
    body: ResetConfirm,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Reset all application settings to defaults. Preserves api_key_hash."""
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail='Reset requires confirmation. Send { "confirm": true } in the request body.',
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    preserved_keys = {"api_key_hash", "encryption_password", "restic_password", "setup_completed", "setup_completed_at", "api_key"}
    placeholders = ", ".join("?" for _ in preserved_keys)  # nosec B608
    await db.execute(
        f"DELETE FROM settings WHERE key NOT IN ({placeholders})",  # nosec B608
        tuple(preserved_keys),
    )

    # Re-insert defaults
    defaults = [
        ("setup_completed", "true", 0),
        ("theme", "dark", 0),
        ("timezone", "UTC", 0),
        ("log_level", "INFO", 0),
        ("keep_daily", "7", 0),
        ("keep_weekly", "4", 0),
        ("keep_monthly", "6", 0),
    ]
    for key, value, encrypted in defaults:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
            (key, value, encrypted, now),
        )

    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("settings", "reset", "All settings reset to defaults",
         json.dumps({"preserved": sorted(preserved_keys), "reset_at": now}),
         "warning", now),
    )
    await db.commit()

    logger.info("All settings reset to defaults (auth & encryption keys preserved)")
    return {"message": "Settings reset to defaults successfully", "preserved": sorted(preserved_keys), "reset_at": now}


# ---------------------------------------------------------------------------
# Cron Preview
# ---------------------------------------------------------------------------


@router.get("/cron-preview")
async def cron_preview(
    expr: str | None = Query(default=None, description="Cron expression to preview"),
    cron: str | None = Query(default=None, description="Cron expression (alias for expr)"),
):
    """Parse a cron expression and return the next 3 scheduled run times.

    Accepts both ``?expr=`` (per spec) and ``?cron=`` (legacy) query params.
    """
    cron_expr = expr or cron
    if not cron_expr:
        raise HTTPException(
            status_code=400,
            detail="Missing required query parameter 'expr' (cron expression).",
        )

    if not croniter.is_valid(cron_expr):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cron expression: '{cron_expr}'. Expected 5-field format.",
        )

    try:
        now = datetime.now(timezone.utc)
        cron_iter = croniter(cron_expr, now)
        next_runs = []
        for _ in range(3):
            next_time = cron_iter.get_next(datetime)
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=timezone.utc)
            next_runs.append(next_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        return {"cron": cron_expr, "next_runs": next_runs, "count": len(next_runs)}
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"Error parsing cron expression: {exc}")


# ---------------------------------------------------------------------------
# Config Export / Import (YAML)
# ---------------------------------------------------------------------------


async def _get_all_settings_for_export(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT key, value, encrypted FROM settings ORDER BY key")
    rows = await cursor.fetchall()
    return [
        {"key": row["key"], "value": row["value"], "encrypted": bool(row["encrypted"])}
        for row in rows
        if row["key"] not in EXPORT_EXCLUDED_KEYS
    ]


def _redact_export_config(config: dict) -> dict:
    """Redact secrets in target config for export."""
    redacted = {}
    sensitive_keys = {"key", "secret", "password", "token", "app_key", "secret_key", "client_secret"}
    for k, v in config.items():
        if any(s in k.lower() for s in sensitive_keys):
            redacted[k] = REDACTED
        else:
            redacted[k] = v
    return redacted


async def _get_all_targets_for_export(db: aiosqlite.Connection) -> list[dict]:
    config = get_config()
    cursor = await db.execute("SELECT * FROM storage_targets ORDER BY created_at")
    rows = await cursor.fetchall()
    targets = []
    for row in rows:
        t = dict(row)
        t["config"] = _redact_export_config(
            decrypt_config(t.get("config", "{}"), str(config.config_dir))
        )
        t["enabled"] = bool(t.get("enabled", 1))
        targets.append(t)
    return targets


async def _get_all_jobs_for_export(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM backup_jobs ORDER BY created_at")
    rows = await cursor.fetchall()
    jobs = []
    for row in rows:
        j = dict(row)
        for f in ("targets", "directories", "exclude_patterns"):
            if f in j and isinstance(j[f], str):
                try:
                    j[f] = json.loads(j[f])
                except (json.JSONDecodeError, TypeError):
                    j[f] = []
        for f in ("enabled", "include_databases", "include_flash"):
            if f in j and isinstance(j[f], int):
                j[f] = bool(j[f])
        jobs.append(j)
    return jobs


async def _get_all_directories_for_export(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM watched_directories ORDER BY path")
    rows = await cursor.fetchall()
    dirs = []
    for row in rows:
        d = dict(row)
        try:
            d["exclude_patterns"] = json.loads(d.get("exclude_patterns", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["exclude_patterns"] = []
        d["enabled"] = bool(d.get("enabled", 1))
        dirs.append(d)
    return dirs


async def _get_all_notifications_for_export(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM notification_channels ORDER BY created_at")
    rows = await cursor.fetchall()
    channels = []
    for row in rows:
        ch = dict(row)
        try:
            ch["events"] = json.loads(ch.get("events", "[]")) if isinstance(ch.get("events"), str) else ch.get("events", [])
        except (json.JSONDecodeError, TypeError):
            ch["events"] = []
        ch["enabled"] = bool(ch.get("enabled", 1))
        channels.append(ch)
    return channels


@router.get("/export")
async def export_config(db: aiosqlite.Connection = Depends(get_db)):
    """Export the entire Arkive configuration as a YAML file."""
    config = {
        "arkive_config": {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "settings": await _get_all_settings_for_export(db),
            "storage_targets": await _get_all_targets_for_export(db),
            "backup_jobs": await _get_all_jobs_for_export(db),
            "watched_directories": await _get_all_directories_for_export(db),
            "notification_channels": await _get_all_notifications_for_export(db),
        }
    }

    yaml_content = yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)
    logger.info("Config exported as YAML")

    return Response(
        content=yaml_content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=arkive-config.yaml"},
    )


# Placeholders produced by export/list redaction. A secret with one of these
# values was never really exported and must not overwrite the stored secret.
_REDACTED_PLACEHOLDERS = {REDACTED, "***", "••••••", "[REDACTED]", "***REDACTED***"}
_VALID_JOB_TYPES = {"full", "db_dump", "flash"}
_BLOCKED_DIR_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot",
    "/proc", "/sys", "/dev", "/var/run", "/root",
)
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _is_redacted_placeholder(value) -> bool:
    return isinstance(value, str) and value.strip() in _REDACTED_PLACEHOLDERS


def _safe_id(raw) -> str:
    candidate = str(raw or "").strip()
    return candidate if _SAFE_ID_RE.match(candidate) else str(uuid.uuid4())[:8]


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _target_config_from_import(raw) -> dict:
    """Imported target config: a dict (export format) or a JSON/encrypted string."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            return decrypt_config(raw, str(get_config().config_dir))
        except Exception:
            return {}
    return {}


async def _reload_scheduler_jobs(scheduler, old_job_ids: set[str], jobs: list[dict]) -> None:
    """Re-register imported jobs with the live scheduler (best effort)."""
    if scheduler is None:
        return
    remove = getattr(scheduler, "remove_job", None)
    add = getattr(scheduler, "add_job", None)
    reschedule = getattr(scheduler, "reschedule_job", None)
    for job_id in old_job_ids:
        if callable(remove):
            try:
                await remove(job_id)
            except Exception as exc:
                logger.warning("Config import: failed to unschedule job %s: %s", job_id, exc)
    for job in jobs:
        if not job.get("enabled"):
            continue
        try:
            if callable(add):
                await add({
                    "id": job["id"],
                    "name": job["name"],
                    "schedule": job["schedule"],
                    "type": job["type"],
                    "enabled": True,
                })
            elif callable(reschedule):
                await reschedule(job["id"], job["schedule"])
        except Exception as exc:
            logger.warning("Config import: failed to schedule job %s: %s", job["id"], exc)


@router.post("/import")
async def import_config(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    cloud_manager=Depends(get_cloud_manager),
):
    """Import an Arkive configuration from a YAML file.

    Each section present in the file replaces that section in the database.
    Secrets that were redacted on export (e.g. ``********``) are merged from
    the existing target with the same id instead of overwriting it; invalid
    entries are skipped and reported in ``warnings``.
    """
    from app.api.targets import (
        VALID_TYPES,
        _control_char_errors,
        _normalize_config,
        _sanitize_name,
        _validate_provider_config,
    )
    from app.models.notifications import VALID_NOTIFICATION_TYPES

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        config = yaml.safe_load(body.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Request body must be valid UTF-8")

    if not isinstance(config, dict) or not isinstance(config.get("arkive_config"), dict):
        raise HTTPException(status_code=400, detail="Invalid config format. Expected 'arkive_config' root key.")

    arkive_config = config["arkive_config"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    app_config = get_config()
    config_dir = str(app_config.config_dir)
    warnings: list[str] = []

    restored = {
        "settings": 0,
        "storage_targets": 0,
        "backup_jobs": 0,
        "watched_directories": 0,
        "notification_channels": 0,
    }

    def _section(name: str) -> list:
        value = arkive_config.get(name, [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise HTTPException(status_code=400, detail=f"'{name}' must be a list")
        return [item for item in value if isinstance(item, dict)]

    # --- Settings ---
    for setting in _section("settings"):
        key = setting.get("key")
        if not isinstance(key, str) or not key or key in EXPORT_EXCLUDED_KEYS:
            continue
        # Block importing protected/readonly settings to prevent privilege escalation
        if key in READONLY_SETTINGS:
            logger.warning("Config import: skipping protected setting '%s'", key)
            continue
        if _CONTROL_RE.search(key):
            warnings.append("Skipped a setting with an invalid key")
            continue
        try:
            val_str = _validate_setting_value(key, setting.get("value"))
        except HTTPException as exc:
            logger.warning("Config import: skipping invalid setting '%s': %s", key, exc.detail)
            warnings.append(f"Skipped setting '{key}': {exc.detail}")
            continue
        encrypted = 1 if setting.get("encrypted", False) else 0

        cursor = await db.execute("SELECT key FROM settings WHERE key = ?", (key,))
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                "UPDATE settings SET value = ?, encrypted = ?, updated_at = ? WHERE key = ?",
                (val_str, encrypted, now, key),
            )
        else:
            await db.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?)",
                (key, val_str, encrypted, now),
            )
        restored["settings"] += 1

    # --- Storage Targets ---
    rclone_writes: list[dict] = []
    removed_target_ids: set[str] = set()
    imported_targets = _section("storage_targets")
    if imported_targets:
        cursor = await db.execute("SELECT * FROM storage_targets")
        existing_targets = {row["id"]: dict(row) for row in await cursor.fetchall()}
        kept_ids: set[str] = set()
        for t in imported_targets:
            tid = _safe_id(t.get("id"))
            if tid in kept_ids:
                tid = str(uuid.uuid4())[:8]
            ttype = str(t.get("type") or "").strip()
            name = _sanitize_name(str(t.get("name") or "Unnamed"))[:50] or "Unnamed"
            if ttype not in VALID_TYPES:
                warnings.append(f"Skipped target '{name}': unsupported type '{ttype}'")
                continue
            cfg = _normalize_config(_target_config_from_import(t.get("config")))
            cc_errors = _control_char_errors(cfg)
            if cc_errors:
                warnings.append(f"Skipped target '{name}': {'; '.join(cc_errors)}")
                continue

            existing_cfg: dict = {}
            if tid in existing_targets:
                try:
                    existing_cfg = decrypt_config(existing_targets[tid].get("config", "{}"), config_dir)
                except Exception:
                    existing_cfg = {}
            missing_secrets: list[str] = []
            for key, value in list(cfg.items()):
                if _is_redacted_placeholder(value):
                    if existing_cfg.get(key) not in (None, "") and not _is_redacted_placeholder(existing_cfg.get(key)):
                        cfg[key] = existing_cfg[key]
                    else:
                        cfg.pop(key)
                        missing_secrets.append(key)

            status = str(t.get("status") or "unknown")
            provider_errors = _validate_provider_config(ttype, cfg)
            if missing_secrets:
                status = "needs_credentials"
                warnings.append(
                    f"Target '{name}' ({tid}) is missing secrets that were redacted on export: "
                    f"{', '.join(sorted(missing_secrets))}. Re-enter them before the next backup."
                )
            elif provider_errors:
                status = "error"
                warnings.append(f"Target '{name}' ({tid}) has an incomplete config: {'; '.join(provider_errors)}")

            await db.execute(
                """INSERT INTO storage_targets
                   (id, name, type, enabled, config, status, last_tested,
                    snapshot_count, total_size_bytes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, type = excluded.type, enabled = excluded.enabled,
                     config = excluded.config, status = excluded.status,
                     updated_at = excluded.updated_at""",
                (tid, name, ttype, 1 if t.get("enabled", True) else 0,
                 encrypt_config(cfg, config_dir), status, t.get("last_tested"),
                 _as_int(t.get("snapshot_count")), _as_int(t.get("total_size_bytes")),
                 str(t.get("created_at") or now), now),
            )
            kept_ids.add(tid)
            restored["storage_targets"] += 1
            if ttype != "local" and not missing_secrets and not provider_errors:
                rclone_writes.append({"id": tid, "type": ttype, "config": cfg})

        removed_target_ids = set(existing_targets) - kept_ids
        for tid in removed_target_ids:
            await db.execute("DELETE FROM storage_targets WHERE id = ?", (tid,))
            await db.execute("DELETE FROM snapshots WHERE target_id = ?", (tid,))
            await db.execute("DELETE FROM size_history WHERE target_id = ?", (tid,))

    # --- Backup Jobs ---
    old_job_ids: set[str] = set()
    imported_job_rows: list[dict] = []
    imported_jobs = _section("backup_jobs")
    if imported_jobs:
        cursor = await db.execute("SELECT id FROM backup_jobs")
        old_job_ids = {row["id"] for row in await cursor.fetchall()}
        kept_job_ids: set[str] = set()
        for j in imported_jobs:
            jid = _safe_id(j.get("id"))
            if jid in kept_job_ids:
                jid = str(uuid.uuid4())[:8]
            name = _CONTROL_RE.sub("", str(j.get("name") or "Unnamed Job"))[:100] or "Unnamed Job"
            jtype = str(j.get("type") or "full")
            schedule = str(j.get("schedule") or "0 2 * * *").strip()
            if jtype not in _VALID_JOB_TYPES:
                warnings.append(f"Skipped job '{name}': unsupported type '{jtype}'")
                continue
            if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
                warnings.append(f"Skipped job '{name}': invalid schedule '{schedule}'")
                continue
            targets = [str(x) for x in _as_list(j.get("targets"))]
            directories = [str(x) for x in _as_list(j.get("directories"))]
            excludes = [str(x) for x in _as_list(j.get("exclude_patterns"))]
            if any(d.startswith("-") or _CONTROL_RE.search(d) for d in directories):
                warnings.append(f"Skipped job '{name}': invalid directory path")
                continue
            enabled = bool(j.get("enabled", True))
            await db.execute(
                """INSERT INTO backup_jobs
                   (id, name, type, schedule, enabled, targets, directories,
                    exclude_patterns, include_databases, include_flash,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name, type = excluded.type, schedule = excluded.schedule,
                     enabled = excluded.enabled, targets = excluded.targets,
                     directories = excluded.directories, exclude_patterns = excluded.exclude_patterns,
                     include_databases = excluded.include_databases,
                     include_flash = excluded.include_flash, updated_at = excluded.updated_at""",
                (jid, name, jtype, schedule, 1 if enabled else 0,
                 json.dumps(targets), json.dumps(directories), json.dumps(excludes),
                 1 if j.get("include_databases", True) else 0,
                 1 if j.get("include_flash", True) else 0,
                 str(j.get("created_at") or now), now),
            )
            kept_job_ids.add(jid)
            imported_job_rows.append({"id": jid, "name": name, "type": jtype, "schedule": schedule, "enabled": enabled})
            restored["backup_jobs"] += 1
        for jid in old_job_ids - kept_job_ids:
            await db.execute("DELETE FROM backup_jobs WHERE id = ?", (jid,))

    # --- Watched Directories ---
    imported_dirs = _section("watched_directories")
    if imported_dirs:
        await db.execute("DELETE FROM watched_directories")
        seen_paths: set[str] = set()
        for d in imported_dirs:
            raw_path = str(d.get("path") or "")
            path = os.path.normpath(raw_path) if raw_path else ""
            if (
                not path.startswith("/")
                or _CONTROL_RE.search(path)
                or any(path == p or path.startswith(p + "/") for p in _BLOCKED_DIR_PREFIXES)
            ):
                warnings.append(f"Skipped watched directory with invalid path: {raw_path!r}")
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            await db.execute(
                """INSERT INTO watched_directories
                   (id, path, label, exclude_patterns, enabled, size_bytes,
                    file_count, last_scanned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_safe_id(d.get("id")), path,
                 _CONTROL_RE.sub("", str(d.get("label") or os.path.basename(path) or "Unnamed")),
                 json.dumps([str(x) for x in _as_list(d.get("exclude_patterns"))]),
                 1 if d.get("enabled", True) else 0,
                 d.get("size_bytes"), d.get("file_count"),
                 d.get("last_scanned"), str(d.get("created_at") or now)),
            )
            restored["watched_directories"] += 1

    # --- Notification Channels ---
    imported_channels = _section("notification_channels")
    if imported_channels:
        await db.execute("DELETE FROM notification_channels")
        for ch in imported_channels:
            name = _CONTROL_RE.sub("", str(ch.get("name") or "Unnamed"))[:50] or "Unnamed"
            ctype = str(ch.get("type") or "webhook")
            if ctype not in VALID_NOTIFICATION_TYPES:
                warnings.append(f"Skipped notification channel '{name}': unsupported type '{ctype}'")
                continue
            cfg = ch.get("config", {})
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg) if cfg else {}
                except (json.JSONDecodeError, TypeError):
                    cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            url = cfg.get("url", "")
            if isinstance(url, str) and url and not is_encrypted(url):
                if _is_redacted_placeholder(url):
                    warnings.append(f"Notification channel '{name}' has no URL (redacted on export); re-enter it")
                    url = ""
                else:
                    url = encrypt_value(url)
            elif isinstance(url, str) and url:
                try:
                    decrypt_value(url)
                except Exception:
                    warnings.append(
                        f"Notification channel '{name}' URL was encrypted with a different key and "
                        "cannot be decrypted here; re-enter it"
                    )
                    url = ""
            cfg["url"] = url if isinstance(url, str) else ""
            events = [str(e) for e in _as_list(ch.get("events"))]
            await db.execute(
                """INSERT INTO notification_channels
                   (id, type, name, enabled, config, events, last_sent,
                    last_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_safe_id(ch.get("id")), ctype, name,
                 1 if ch.get("enabled", True) and cfg["url"] else 0,
                 json.dumps(cfg), json.dumps(events),
                 ch.get("last_sent"), ch.get("last_status"),
                 str(ch.get("created_at") or now)),
            )
            restored["notification_channels"] += 1

    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("settings", "imported", "Configuration imported from YAML",
         json.dumps({**restored, "warnings": len(warnings)}), "warning" if warnings else "info", now),
    )
    await db.commit()

    # Post-commit side effects: rclone remotes and the live scheduler.
    if cloud_manager is not None:
        for tid in removed_target_ids:
            try:
                await cloud_manager.remove_target_config(tid)
            except Exception as exc:
                logger.warning("Config import: failed to remove rclone section %s: %s", tid, exc)
        for target in rclone_writes:
            try:
                await cloud_manager.write_target_config(target)
            except Exception as exc:
                warnings.append(f"Target {target['id']}: rclone config could not be written: {exc}")
    if imported_jobs:
        await _reload_scheduler_jobs(getattr(request.app.state, "scheduler", None), old_job_ids, imported_job_rows)

    logger.info("Config imported from YAML: %s (%d warnings)", restored, len(warnings))
    return {
        "message": "Configuration imported successfully",
        "restored": restored,
        "warnings": warnings,
        "imported_at": now,
    }
