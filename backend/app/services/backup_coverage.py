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

    try:
        cursor = await db.execute(
            "SELECT path FROM watched_directories WHERE enabled = 1 ORDER BY path"
        )
        protected_directories = [
            _normalize_path(row["path"] if isinstance(row, aiosqlite.Row) else row[0])
            for row in await cursor.fetchall()
            if (row["path"] if isinstance(row, aiosqlite.Row) else row[0])
        ]
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        protected_directories = []

    protected_set = set(protected_directories)
    appdata_path = _normalize_path(f"{user_shares_path}/appdata")

    appdata_protected = any(
        path == appdata_path or path.startswith(appdata_path + "/")
        for path in protected_set
    )

    flash_protected = False
    try:
        cursor = await db.execute(
            """SELECT flash_backed_up
               FROM job_runs
               WHERE status IN ('success', 'partial')
               ORDER BY started_at DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        flash_protected = bool(row and (row["flash_backed_up"] if isinstance(row, aiosqlite.Row) else row[0]))
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        flash_protected = False

    if not protected_directories:
        warnings.append(
            "No watched directories are configured. Only dump artifacts are protected."
        )

    if platform == "unraid":
        if not appdata_protected:
            warnings.append(
                "Unraid appdata is not protected. Add /mnt/user/appdata for full container restore coverage."
            )
            recommended_directories.append(appdata_path)
        if not flash_protected:
            warnings.append(
                "Unraid flash backup has not completed successfully yet."
            )

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
