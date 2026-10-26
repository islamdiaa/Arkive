"""Databases the user excluded from backups (Databases page).

Some discovered databases aren't worth backing up, or can't be copied
consistently while their app runs: Dropbox keeps an exclusive lock on its 400 MB
sync index, which it rebuilds from the cloud anyway. An excluded database is
never dumped, doesn't count towards database health, and its old dumps age out
like those of a removed container.

Exclusions are stored in the ``excluded_databases`` setting as a JSON list of
database identities (container, type, name, host path).
"""

import json
from typing import Any

import aiosqlite

from app.services.db_dumper import dump_prefix
from app.services.settings_store import get_setting, set_setting

SETTING_KEY = "excluded_databases"
_FIELDS = ("container_name", "db_type", "db_name", "host_path")


def identity(entry: Any) -> str:
    """Stable identity of a database (dict or DiscoveredDatabase)."""

    def field(name: str):
        return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)

    return dump_prefix(
        str(field("container_name") or ""),
        str(field("db_type") or ""),
        str(field("db_name") or ""),
        field("host_path") or None,
    )


async def read_exclusions(db: aiosqlite.Connection) -> list[dict]:
    raw = await get_setting(db, SETTING_KEY, "[]")
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    return [{f: e.get(f) for f in _FIELDS} for e in entries if isinstance(e, dict) and e.get("container_name")]


async def excluded_identities(db: aiosqlite.Connection) -> set[str]:
    return {identity(e) for e in await read_exclusions(db)}


async def set_excluded(db: aiosqlite.Connection, entry: dict, excluded: bool) -> list[dict]:
    """Add or remove ``entry`` from the exclusions; returns the new list."""
    target = {f: entry.get(f) or None for f in _FIELDS}
    key = identity(target)
    entries = [e for e in await read_exclusions(db) if identity(e) != key]
    if excluded:
        entries.append(target)
    await set_setting(db, SETTING_KEY, json.dumps(entries))
    return entries
