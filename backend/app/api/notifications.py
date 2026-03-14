"""Notification channels API routes — CRUD + test send via Apprise."""

import json
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_db, get_notifier, require_auth
from app.core.security import decrypt_value, encrypt_value
from app.models.notifications import (
    NotificationChannelCreate,
    NotificationChannelUpdate,
)

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
async def list_channels(
    limit: int = 200,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all notification channels with redacted URLs."""
    cursor = await db.execute("SELECT COUNT(*) FROM notification_channels")
    total = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT * FROM notification_channels ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cursor.fetchall()

    channels = []
    for row in rows:
        ch = dict(row)
        ch["events"] = json.loads(ch.get("events") or "[]")
        config_data = json.loads(ch.get("config") or "{}")
        ch["enabled"] = bool(ch["enabled"])

        # Decrypt URL then redact for list response
        if "url" in config_data:
            decrypted_url = decrypt_value(config_data["url"])
            config_data["url"] = "••••••" if decrypted_url else ""
        ch["config"] = config_data
        channels.append(ch)

    return {
        "items": channels,
        "channels": channels,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.post("", status_code=201)
async def create_channel(
    body: NotificationChannelCreate,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Create a new notification channel."""
    channel_id = str(uuid.uuid4())[:8]
    config = json.dumps({"url": encrypt_value(body.url)})

    await db.execute(
        """INSERT INTO notification_channels (id, type, name, enabled, config, events)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            channel_id,
            body.type,
            body.name,
            int(body.enabled),
            config,
            json.dumps(body.events),
        ),
    )
    await db.commit()
    return {"id": channel_id, "name": body.name}


@router.put("/{channel_id}")
async def update_channel(
    channel_id: str,
    body: NotificationChannelUpdate,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Update an existing notification channel."""
    cursor = await db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Channel not found")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.enabled is not None:
        updates["enabled"] = int(body.enabled)
    if body.events is not None:
        updates["events"] = json.dumps(body.events)
    if body.url is not None:
        updates["config"] = json.dumps({"url": encrypt_value(body.url)})

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)  # nosec B608
        await db.execute(
            f"UPDATE notification_channels SET {set_clause} WHERE id = ?",  # nosec B608
            (*updates.values(), channel_id),
        )
        await db.commit()

    return {"status": "updated"}


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Delete a notification channel."""
    cursor = await db.execute("SELECT id FROM notification_channels WHERE id = ?", (channel_id,))
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Channel not found")
    await db.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
    await db.commit()
    return {"status": "deleted"}


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    notifier=Depends(get_notifier),
):
    """Send a test notification to a channel."""
    cursor = await db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")

    config = json.loads(dict(row).get("config") or "{}")
    url = decrypt_value(config.get("url", ""))
    result = await notifier.test_channel(url)
    return result
