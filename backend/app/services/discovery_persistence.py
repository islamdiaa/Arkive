"""Shared persistence for discovery scan results."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite


async def persist_discovery_results(
    db: aiosqlite.Connection,
    containers: list,
) -> None:
    """Persist discovery scan results into discovered_containers."""
    current_names = {c.name for c in containers}
    scanned_at = datetime.now(UTC).isoformat()

    for c in containers:
        await db.execute(
            """INSERT OR REPLACE INTO discovered_containers
            (name, image, status, ports, mounts, databases, profile, priority, compose_project, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c.name,
                c.image,
                c.status,
                json.dumps(c.ports),
                json.dumps(c.mounts),
                json.dumps([d.model_dump() for d in c.databases]),
                c.profile,
                c.priority,
                c.compose_project,
                scanned_at,
            ),
        )

    if current_names:
        placeholders = ",".join("?" for _ in current_names)  # nosec B608
        await db.execute(
            f"DELETE FROM discovered_containers WHERE name NOT IN ({placeholders})",  # nosec B608
            tuple(current_names),
        )
    else:
        await db.execute("DELETE FROM discovered_containers")
