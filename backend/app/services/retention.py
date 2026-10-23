"""Retention policy settings shared by the orchestrator and scheduler."""

import aiosqlite

DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 6


async def read_retention_settings(db: aiosqlite.Connection) -> tuple[int, int, int]:
    """Return (keep_daily, keep_weekly, keep_monthly) from the settings table.

    Values below 1 or unparsable values fall back to the defaults — restic
    would otherwise forget every snapshot in that bucket.
    """
    values = {
        "keep_daily": DEFAULT_KEEP_DAILY,
        "keep_weekly": DEFAULT_KEEP_WEEKLY,
        "keep_monthly": DEFAULT_KEEP_MONTHLY,
    }
    cursor = await db.execute(
        "SELECT key, value FROM settings WHERE key IN ('keep_daily', 'keep_weekly', 'keep_monthly')"
    )
    for key, raw in await cursor.fetchall():
        try:
            val = int(raw)
        except (ValueError, TypeError):
            continue
        if val >= 1:
            values[key] = val
    return values["keep_daily"], values["keep_weekly"], values["keep_monthly"]
