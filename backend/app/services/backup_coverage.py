"""Backup coverage evaluation for migration readiness warnings."""

from __future__ import annotations

import sqlite3

import aiosqlite


def _normalize_path(path: str) -> str:
    return path.rstrip("/") or path


async def evaluate_backup_coverage(
    db: aiosqlite.Connection,
    *,
    platform: str,
    user_shares_path: str = "/mnt/user",
) -> dict:
    """Return backup coverage details separate from operational health.

    Operational health answers whether Arkive is functioning.
    Coverage answers whether enough filesystem state is protected to restore
    a server onto new hardware with high fidelity.
    """
    warnings: list[str] = []
    recommended_directories: list[str] = []
    protected_directories: list[str] = []
    latest_directory_change: str | None = None

    try:
        cursor = await db.execute("SELECT path, created_at FROM watched_directories WHERE enabled = 1 ORDER BY path")
        rows = await cursor.fetchall()
        for row in rows:
            path = row["path"] if isinstance(row, aiosqlite.Row) else row[0]
            created_at = row["created_at"] if isinstance(row, aiosqlite.Row) else row[1]
            if path:
                protected_directories.append(_normalize_path(path))
            if created_at and (latest_directory_change is None or created_at > latest_directory_change):
                latest_directory_change = created_at
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        protected_directories = []
        latest_directory_change = None

    protected_set = set(protected_directories)
    appdata_path = _normalize_path(f"{user_shares_path}/appdata")

    appdata_configured = any(path == appdata_path or path.startswith(appdata_path + "/") for path in protected_set)
    appdata_protected = appdata_configured

    latest_successful_backup_started_at: str | None = None
    try:
        cursor = await db.execute(
            """SELECT started_at
               FROM job_runs
               WHERE status IN ('success', 'partial')
               ORDER BY started_at DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        latest_successful_backup_started_at = (
            row["started_at"] if row and isinstance(row, aiosqlite.Row) else row[0] if row else None
        )
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        latest_successful_backup_started_at = None

    directories_backed_up = (
        bool(protected_directories)
        and bool(latest_successful_backup_started_at)
        and (latest_directory_change is None or latest_successful_backup_started_at >= latest_directory_change)
    )
    if protected_directories and not directories_backed_up:
        warnings.append("Watched directories were added or changed, but no successful backup has captured them yet.")
    if appdata_configured and not directories_backed_up:
        appdata_protected = False

    flash_protected = False
    try:
        cursor = await db.execute(
            """SELECT 1
               FROM job_runs
               WHERE status IN ('success', 'partial') AND flash_backed_up = 1
               ORDER BY started_at DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        flash_protected = bool(row)
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        flash_protected = False

    if not protected_directories:
        warnings.append("No watched directories are configured. Only dump artifacts are protected.")

    if platform == "unraid":
        if not appdata_configured:
            warnings.append(
                "Unraid appdata is not protected. Add /mnt/user/appdata for full container restore coverage."
            )
            recommended_directories.append(appdata_path)
        elif not appdata_protected:
            warnings.append("Unraid appdata is configured, but it has not been captured by a successful backup yet.")
        if not flash_protected:
            warnings.append("Unraid flash backup has not completed successfully yet.")

    readiness = "migration_ready"
    if warnings:
        readiness = "partial"
    if not protected_directories and platform != "unraid":
        readiness = "minimal"

    return {
        "readiness": readiness,
        "migration_ready": not warnings,
        "appdata_protected": appdata_protected,
        "flash_protected": flash_protected,
        "watched_directories": len(protected_directories),
        "protected_directories": protected_directories,
        "recommended_directories": recommended_directories,
        "warnings": warnings,
    }
