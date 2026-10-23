"""Typed reads of individual rows in the settings table.

Settings are stored as strings; these helpers parse them and fall back to the
default on a missing row, bad data, or a not-yet-migrated database.
"""

import sqlite3

import aiosqlite

# Defaults for settings read outside the settings API.
DEFAULTS: dict[str, str] = {
    "heartbeat_url": "",
    "stale_backup_hours": "0",  # 0 = derive from each job's schedule
    "dump_compression": "none",  # none | gzip
    "restore_drill_enabled": "true",
    "restore_drill_schedule": "0 7 * * 0",
    "restore_drill_max_databases": "3",
    "restore_drill_max_dump_mb": "2048",
}


async def get_setting(db: aiosqlite.Connection, key: str, default: str | None = None) -> str:
    """Return the raw string value of ``key`` (or its default)."""
    fallback = DEFAULTS.get(key, "") if default is None else default
    try:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        return fallback
    if not row or row[0] is None:
        return fallback
    return str(row[0])


async def get_int_setting(
    db: aiosqlite.Connection, key: str, default: int | None = None, minimum: int | None = None
) -> int:
    fallback = int(DEFAULTS.get(key, "0")) if default is None else default
    try:
        value = int(await get_setting(db, key, str(fallback)))
    except (TypeError, ValueError):
        return fallback
    if minimum is not None and value < minimum:
        return fallback
    return value


async def get_bool_setting(db: aiosqlite.Connection, key: str, default: bool | None = None) -> bool:
    fallback = DEFAULTS.get(key, "false") == "true" if default is None else default
    raw = (await get_setting(db, key, "true" if fallback else "false")).strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    return fallback


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        """INSERT INTO settings (key, value, encrypted, updated_at)
           VALUES (?, ?, 0, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )
    await db.commit()
