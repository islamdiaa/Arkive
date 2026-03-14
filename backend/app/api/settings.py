"""
Settings API endpoints.
CRUD for application settings with secret redaction.
Includes config export/import as YAML and cron preview.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime

import aiosqlite
import yaml
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.dependencies import get_config, get_db, get_event_bus, require_auth
from app.core.security import decrypt_config

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


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def get_settings(db: aiosqlite.Connection = Depends(get_db)):
    """Get all application settings with sensitive values redacted."""
    cursor = await db.execute("SELECT key, value, encrypted, updated_at FROM settings ORDER BY key")
    rows = await cursor.fetchall()

    items = []
    values: dict[str, str] = {}
    for row in rows:
        key = row["key"]
        sensitive = _is_sensitive(key, row["encrypted"])
        values[key] = row["value"]
        items.append(
            {
                "key": key,
                "value": REDACTED if sensitive else row["value"],
                "encrypted": bool(row["encrypted"]),
                "sensitive": sensitive,
                "has_value": bool(row["value"]),
                "updated_at": row["updated_at"],
            }
        )

    keep_daily = int(values.get("keep_daily", "7") or 7)
    keep_weekly = int(values.get("keep_weekly", "4") or 4)
    keep_monthly = int(values.get("keep_monthly", "6") or 6)
    return {
        "items": items,
        "total": len(items),
        "server_name": values.get("server_name", ""),
        "timezone": values.get("timezone", "UTC"),
        "retention_days": int(values.get("retention_days", "30") or 30),
        "keep_daily": keep_daily,
        "keep_weekly": keep_weekly,
        "keep_monthly": keep_monthly,
        "log_level": values.get("log_level", "INFO"),
        "web_url": values.get("web_url", ""),
        "theme": values.get("theme", "dark"),
        "bandwidth_limit": values.get("bandwidth_limit", ""),
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
    "server_name",
    "timezone",
    "theme",
    "log_level",
    "web_url",
    "retention_days",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "flash_retention",
    "backup_schedule",
    "db_dump_schedule",
    "cloud_sync_schedule",
    "flash_schedule",
    "notify_on_success",
    "notify_on_failure",
    "min_disk_space_bytes",
    "warn_disk_space_bytes",
    "bandwidth_limit",
}
READONLY_SETTINGS = {
    "api_key_hash",
    "api_key",
    "encryption_password",
    "restic_password",
    "setup_completed_at",
    "setup_completed",
    "platform",
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

    # Validate timezone
    if "timezone" in settings_payload:
        tz_val = settings_payload["timezone"]
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(tz_val)
        except (KeyError, Exception):
            raise HTTPException(status_code=422, detail=f"Invalid timezone: '{tz_val}'")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = []

    # Validate retention values
    retention_keys = {"retention_days", "keep_daily", "keep_weekly", "keep_monthly", "flash_retention"}
    for key, value in settings_payload.items():
        if key in retention_keys:
            try:
                int_val = int(value)
                if int_val < 1:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid value for '{key}': must be a positive integer (got {value})",
                    )
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid value for '{key}': must be a positive integer (got {value})",
                )

    # Validate disk space values
    disk_space_keys = {"min_disk_space_bytes", "warn_disk_space_bytes"}
    for key, value in settings_payload.items():
        if key in disk_space_keys:
            try:
                int_val = int(value)
                if int_val < 0:
                    raise HTTPException(status_code=422, detail=f"'{key}' must be non-negative")
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"'{key}' must be an integer")

    # Validate bandwidth_limit (restic --limit-upload expects a positive integer in KiB/s)
    if "bandwidth_limit" in settings_payload:
        bw_val = str(settings_payload["bandwidth_limit"]) if settings_payload["bandwidth_limit"] is not None else ""
        if bw_val and not _BANDWIDTH_RE.match(bw_val):
            raise HTTPException(
                status_code=422,
                detail="bandwidth_limit must be a positive integer (KiB/s) or empty to disable",
            )

    for key, value in settings_payload.items():
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

        updated.append(
            {
                "key": key,
                "value": REDACTED if _is_sensitive(key, encrypted) else value,
                "encrypted": bool(encrypted),
                "sensitive": _is_sensitive(key, encrypted),
                "updated_at": now,
            }
        )

    await db.commit()

    if event_bus is not None:
        await event_bus.publish("settings.updated", {"count": len(updated)})

    logger.info("Bulk settings update: %d settings updated", len(updated))
    return {"items": updated, "total": len(updated), "updated_at": now}


@router.put("/{key}")
async def update_setting(
    key: str,
    body: SettingUpdate,
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

    # Validate timezone
    if key == "timezone":
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(body.value)
        except (KeyError, Exception):
            raise HTTPException(status_code=422, detail=f"Invalid timezone: '{body.value}'")

    # Validate retention values
    if key in {"retention_days", "keep_daily", "keep_weekly", "keep_monthly", "flash_retention"}:
        try:
            int_val = int(body.value)
            if int_val < 1:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid value for '{key}': must be a positive integer (got {body.value})",
                )
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid value for '{key}': must be a positive integer (got {body.value})",
            )

    # Validate disk space values
    if key in {"min_disk_space_bytes", "warn_disk_space_bytes"}:
        try:
            int_val = int(body.value)
            if int_val < 0:
                raise HTTPException(status_code=422, detail=f"'{key}' must be non-negative")
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"'{key}' must be an integer")

    # Validate bandwidth_limit (restic --limit-upload expects a positive integer in KiB/s)
    if key == "bandwidth_limit":
        bw_val = str(body.value) if body.value is not None else ""
        if bw_val and not _BANDWIDTH_RE.match(bw_val):
            raise HTTPException(
                status_code=422,
                detail="bandwidth_limit must be a positive integer (KiB/s) or empty to disable",
            )

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    return {
        "key": key,
        "value": REDACTED if _is_sensitive(key, encrypted) else body.value,
        "encrypted": bool(encrypted),
        "sensitive": _is_sensitive(key, encrypted),
        "updated_at": now,
    }


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

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    preserved_keys = {
        "api_key_hash",
        "encryption_password",
        "restic_password",
        "setup_completed",
        "setup_completed_at",
        "api_key",
    }
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
        (
            "settings",
            "reset",
            "All settings reset to defaults",
            json.dumps({"preserved": sorted(preserved_keys), "reset_at": now}),
            "warning",
            now,
        ),
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
        now = datetime.now(UTC)
        cron_iter = croniter(cron_expr, now)
        next_runs = []
        for _ in range(3):
            next_time = cron_iter.get_next(datetime)
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=UTC)
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
        t["config"] = _redact_export_config(decrypt_config(t.get("config", "{}"), str(config.config_dir)))
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
            ch["events"] = (
                json.loads(ch.get("events", "[]")) if isinstance(ch.get("events"), str) else ch.get("events", [])
            )
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
            "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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


@router.post("/import")
async def import_config(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Import an Arkive configuration from a YAML file."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        config = yaml.safe_load(body.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Request body must be valid UTF-8")

    if not config or "arkive_config" not in config:
        raise HTTPException(status_code=400, detail="Invalid config format. Expected 'arkive_config' root key.")

    arkive_config = config["arkive_config"]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    restored = {
        "settings": 0,
        "storage_targets": 0,
        "backup_jobs": 0,
        "watched_directories": 0,
        "notification_channels": 0,
    }

    # --- Settings ---
    for setting in arkive_config.get("settings", []):
        key = setting.get("key")
        value = setting.get("value")
        encrypted = 1 if setting.get("encrypted", False) else 0
        if not key or key in EXPORT_EXCLUDED_KEYS:
            continue
        # Block importing protected/readonly settings to prevent privilege escalation
        if key in READONLY_SETTINGS:
            logger.warning("Config import: skipping protected setting '%s'", key)
            continue

        # Validate bandwidth_limit format to reject corrupt/malicious values
        if key == "bandwidth_limit":
            val_for_check = str(value) if value is not None else ""
            if not _BANDWIDTH_RE.match(val_for_check):
                logger.warning("Config import: skipping invalid bandwidth_limit '%s'", val_for_check)
                continue

        cursor = await db.execute("SELECT key FROM settings WHERE key = ?", (key,))
        existing = await cursor.fetchone()
        val_str = str(value) if value is not None else ""

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
    imported_targets = arkive_config.get("storage_targets", [])
    if imported_targets:
        await db.execute("DELETE FROM storage_targets")
        for t in imported_targets:
            tid = t.get("id", str(uuid.uuid4())[:8])
            cfg = t.get("config", {})
            cfg_str = json.dumps(cfg) if isinstance(cfg, dict) else str(cfg)
            await db.execute(
                """INSERT INTO storage_targets
                   (id, name, type, enabled, config, status, last_tested,
                    snapshot_count, total_size_bytes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    t.get("name", "Unnamed"),
                    t.get("type", "local"),
                    1 if t.get("enabled", True) else 0,
                    cfg_str,
                    t.get("status", "unknown"),
                    t.get("last_tested"),
                    t.get("snapshot_count"),
                    t.get("total_size_bytes"),
                    t.get("created_at", now),
                    t.get("updated_at", now),
                ),
            )
            restored["storage_targets"] += 1

    # --- Backup Jobs ---
    imported_jobs = arkive_config.get("backup_jobs", [])
    if imported_jobs:
        await db.execute("DELETE FROM backup_jobs")
        for j in imported_jobs:
            jid = j.get("id", str(uuid.uuid4())[:8])
            await db.execute(
                """INSERT INTO backup_jobs
                   (id, name, type, schedule, enabled, targets, directories,
                    exclude_patterns, include_databases, include_flash,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    jid,
                    j.get("name", "Unnamed Job"),
                    j.get("type", "full"),
                    j.get("schedule", "0 2 * * *"),
                    1 if j.get("enabled", True) else 0,
                    json.dumps(j.get("targets", [])) if isinstance(j.get("targets"), list) else j.get("targets", "[]"),
                    json.dumps(j.get("directories", []))
                    if isinstance(j.get("directories"), list)
                    else j.get("directories", "[]"),
                    json.dumps(j.get("exclude_patterns", []))
                    if isinstance(j.get("exclude_patterns"), list)
                    else j.get("exclude_patterns", "[]"),
                    1 if j.get("include_databases", True) else 0,
                    1 if j.get("include_flash", True) else 0,
                    j.get("created_at", now),
                    j.get("updated_at", now),
                ),
            )
            restored["backup_jobs"] += 1

    # --- Watched Directories ---
    imported_dirs = arkive_config.get("watched_directories", [])
    if imported_dirs:
        await db.execute("DELETE FROM watched_directories")
        for d in imported_dirs:
            did = d.get("id", str(uuid.uuid4())[:8])
            await db.execute(
                """INSERT INTO watched_directories
                   (id, path, label, exclude_patterns, enabled, size_bytes,
                    file_count, last_scanned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    did,
                    d.get("path", "/unknown"),
                    d.get("label", "Unnamed"),
                    json.dumps(d.get("exclude_patterns", []))
                    if isinstance(d.get("exclude_patterns"), list)
                    else d.get("exclude_patterns", "[]"),
                    1 if d.get("enabled", True) else 0,
                    d.get("size_bytes"),
                    d.get("file_count"),
                    d.get("last_scanned"),
                    d.get("created_at", now),
                ),
            )
            restored["watched_directories"] += 1

    # --- Notification Channels ---
    imported_channels = arkive_config.get("notification_channels", [])
    if imported_channels:
        await db.execute("DELETE FROM notification_channels")
        for ch in imported_channels:
            chid = ch.get("id", str(uuid.uuid4())[:8])
            cfg = ch.get("config", "{}")
            cfg_str = json.dumps(cfg) if isinstance(cfg, dict) else str(cfg)
            events = ch.get("events", [])
            events_str = json.dumps(events) if isinstance(events, list) else str(events)
            await db.execute(
                """INSERT INTO notification_channels
                   (id, type, name, enabled, config, events, last_sent,
                    last_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chid,
                    ch.get("type", "webhook"),
                    ch.get("name", "Unnamed"),
                    1 if ch.get("enabled", True) else 0,
                    cfg_str,
                    events_str,
                    ch.get("last_sent"),
                    ch.get("last_status"),
                    ch.get("created_at", now),
                ),
            )
            restored["notification_channels"] += 1

    await db.commit()

    # Log import
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("settings", "imported", "Configuration imported from YAML", json.dumps(restored), "info", now),
    )
    await db.commit()

    logger.info("Config imported from YAML: %s", restored)
    return {"message": "Configuration imported successfully", "restored": restored, "imported_at": now}
