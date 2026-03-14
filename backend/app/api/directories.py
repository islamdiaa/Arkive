"""Watched directories API routes."""

import asyncio
import json
import os
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.core.activity import log_activity
from app.core.dependencies import get_db, require_auth
from app.models.discovery import DirectoryCreate

router = APIRouter(prefix="/directories", tags=["directories"], dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# Module-level constants and helpers for directory classification.
# Extracted to module level so they can be imported and unit-tested directly.
# ---------------------------------------------------------------------------

# Directories that are almost always huge media/data stores — skip suggesting.
SKIP_NAMES = {
    "media",
    "movies",
    "tv",
    "downloads",
    "music",
    "audiobooks",
    "transcode",
    "isos",
    "games",
    "torrents",
    "usenet",
    "youtube",
    "podcasts",
    "videos",
    "recordings",
    "rips",
}

# File extensions that indicate re-downloadable media content.
MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".m4v",
    ".ts",  # video
    ".mp3",
    ".flac",
    ".aac",
    ".ogg",
    ".wma",
    ".m4a",
    ".opus",  # audio
    ".iso",
    ".img",
    ".bin",
    ".nrg",  # disk images
    ".rar",
    ".r00",
    ".r01",  # split archives
}

# Size threshold: directories under this are likely config/scripts worth backing up.
SMALL_DIR_THRESHOLD = 1 * 1024 * 1024 * 1024  # 1 GB


def _quick_size(path: str, max_files: int = 10000) -> tuple[int, int]:
    """Walk a directory, capping at *max_files* to stay fast."""
    size = 0
    count = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            count += len(filenames)
            for f in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
            if count >= max_files:
                break
    except PermissionError:
        pass
    return size, count


def _is_media_dominated(path: str, sample_limit: int = 200) -> bool:
    """Check if a directory is dominated by large media files.

    Samples up to *sample_limit* files. If >60% of total size comes from
    media extensions, the directory is considered re-downloadable media.
    """
    media_size = 0
    total_size = 0
    sampled = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    fsize = os.path.getsize(fpath)
                except OSError:
                    continue
                total_size += fsize
                ext = os.path.splitext(fname)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    media_size += fsize
                sampled += 1
                if sampled >= sample_limit:
                    break
            if sampled >= sample_limit:
                break
    except PermissionError:
        pass
    if total_size == 0:
        return False
    return (media_size / total_size) > 0.6


def _human_size(size_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _validate_directory_path(path: str) -> str:
    """Validate and normalize a watched directory path."""
    if not os.path.isabs(path):
        raise HTTPException(400, "Path must be absolute")

    normalized = os.path.normpath(path)
    blocked_prefixes = (
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/boot",
        "/proc",
        "/sys",
        "/dev",
        "/var/run",
        "/root",
    )
    for prefix in blocked_prefixes:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            raise HTTPException(400, f"Cannot watch system directory: {prefix}")

    if not os.path.isdir(normalized):
        raise HTTPException(400, f"Path does not exist: {normalized}")

    return normalized


@router.get("")
async def list_directories(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM watched_directories ORDER BY path")
    rows = await cursor.fetchall()
    dirs = []
    for row in rows:
        d = dict(row)
        d["exclude_patterns"] = json.loads(d.get("exclude_patterns", "[]"))
        d["enabled"] = bool(d["enabled"])
        dirs.append(d)
    total = len(dirs)
    return {"items": dirs, "directories": dirs, "total": total, "limit": 50, "offset": 0, "has_more": False}


@router.post("", status_code=201)
async def add_directory(body: DirectoryCreate, db: aiosqlite.Connection = Depends(get_db)):
    body.path = _validate_directory_path(body.path)

    # Check for duplicate path
    cursor = await db.execute("SELECT id FROM watched_directories WHERE path = ?", (body.path,))
    if await cursor.fetchone():
        raise HTTPException(409, f"Directory already watched: {body.path}")

    dir_id = str(uuid.uuid4())[:8]
    await db.execute(
        """INSERT INTO watched_directories (id, path, label, exclude_patterns, enabled)
        VALUES (?, ?, ?, ?, ?)""",
        (dir_id, body.path, body.label, json.dumps(body.exclude_patterns), int(body.enabled)),
    )
    await db.commit()
    await log_activity(
        db, "directory", "created", f"Added watched directory '{body.label}'", {"dir_id": dir_id, "path": body.path}
    )
    return {"id": dir_id, "path": body.path}


@router.put("/{dir_id}")
async def update_directory(dir_id: str, body: DirectoryCreate, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT * FROM watched_directories WHERE id = ?", (dir_id,))
    if not await cursor.fetchone():
        raise HTTPException(404, "Directory not found")

    body.path = _validate_directory_path(body.path)

    await db.execute(
        """UPDATE watched_directories SET path = ?, label = ?, exclude_patterns = ?, enabled = ?
        WHERE id = ?""",
        (body.path, body.label, json.dumps(body.exclude_patterns), int(body.enabled), dir_id),
    )
    await db.commit()
    return {"status": "updated"}


@router.delete("/{dir_id}")
async def remove_directory(dir_id: str, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("SELECT id FROM watched_directories WHERE id = ?", (dir_id,))
    if not await cursor.fetchone():
        raise HTTPException(404, "Directory not found")
    await db.execute("DELETE FROM watched_directories WHERE id = ?", (dir_id,))
    await db.commit()
    await log_activity(db, "directory", "deleted", f"Removed directory {dir_id}", {"dir_id": dir_id})
    return {"status": "deleted"}


@router.get("/scan")
async def scan_directories_get(db: aiosqlite.Connection = Depends(get_db)):
    """GET alias for directory scan — same as POST /scan."""
    return await scan_directories(db)


@router.post("/scan")
async def scan_directories(db: aiosqlite.Connection = Depends(get_db)):
    """Scan directories and suggest ones to watch.

    Dynamically discovers all top-level ``/mnt/user/`` subdirectories,
    classifies them by size and content, and returns suggestions with
    already-watched status so the frontend can offer one-click add.

    Returns ``{directories: [...], suggestions: [...], platform: "unraid"|"linux"}``.
    """
    from app.core.platform import detect_platform

    # Gather already-watched paths so we can mark them.
    cursor = await db.execute("SELECT path FROM watched_directories")
    watched_paths = {row["path"] for row in await cursor.fetchall()}

    # Well-known paths with curated metadata.
    # Only include paths that are genuinely worth backing up — do NOT include
    # media/isos here, as they should go through the normal skip-name and
    # media-dominated filtering in the dynamic discovery pass.
    known_paths: dict[str, tuple[str, str, list[str]]] = {
        "/mnt/user/appdata": ("Appdata", "critical", ["*.log", "cache/", "thumbs/"]),
        "/mnt/user/system/docker": ("Docker", "recommended", []),
        "/mnt/user/domains": ("VMs", "recommended", []),
    }

    def _build_scan_results() -> tuple[list[dict], list[dict]]:
        """Synchronous scan logic — runs in a thread to avoid blocking the event loop."""
        directories: list[dict] = []
        suggestions: list[dict] = []
        seen_paths: set[str] = set()
        size_cache: dict[str, tuple[int, int]] = {}

        # 1. Emit the legacy hardcoded list (for backward compat).
        for path, (label, priority, excludes) in known_paths.items():
            if os.path.isdir(path):
                size, count = _quick_size(path, max_files=5000)
                size_cache[path] = (size, count)
                directories.append(
                    {
                        "path": path,
                        "label": label,
                        "size_bytes": size,
                        "file_count": count,
                        "priority": priority,
                        "recommended_excludes": excludes,
                    }
                )
                seen_paths.add(path)

        # 2. Dynamically discover all top-level /mnt/user/ subdirectories.
        user_shares_root = "/mnt/user"
        if os.path.isdir(user_shares_root):
            try:
                entries = sorted(os.listdir(user_shares_root))
            except PermissionError:
                entries = []

            for name in entries:
                full_path = os.path.join(user_shares_root, name)
                if not os.path.isdir(full_path):
                    continue
                if full_path in seen_paths:
                    # Already covered by hardcoded list — reuse cached size
                    # and add to suggestions so frontend can show watched status.
                    info = known_paths.get(full_path)
                    if info:
                        label, priority, excludes = info
                        size, count = size_cache.get(full_path, _quick_size(full_path, max_files=5000))
                        suggestions.append(
                            {
                                "path": full_path,
                                "label": label,
                                "size_bytes": size,
                                "file_count": count,
                                "priority": priority,
                                "recommended_excludes": excludes,
                                "already_watched": full_path in watched_paths,
                                "reason": "Well-known Unraid share",
                            }
                        )
                    continue

                seen_paths.add(full_path)

                # Skip known massive media directories by name.
                if name.lower() in SKIP_NAMES:
                    continue

                # Quick-scan to classify.
                size, count = _quick_size(full_path, max_files=5000)

                # Skip large directories dominated by re-downloadable media files.
                if size > SMALL_DIR_THRESHOLD and _is_media_dominated(full_path):
                    continue

                # Determine priority based on size and name heuristics.
                if name.lower() == "appdata":
                    priority = "critical"
                    reason = "Container configuration data"
                    excludes = ["*.log", "cache/", "thumbs/"]
                elif size < SMALL_DIR_THRESHOLD:
                    priority = "recommended"
                    reason = f"Small directory ({_human_size(size)})"
                    excludes = ["*.log"]
                else:
                    priority = "optional"
                    reason = f"Large directory ({_human_size(size)})"
                    excludes = ["*.log", "cache/"]

                label = name.replace("-", " ").replace("_", " ").title()

                suggestions.append(
                    {
                        "path": full_path,
                        "label": label,
                        "size_bytes": size,
                        "file_count": count,
                        "priority": priority,
                        "recommended_excludes": excludes,
                        "already_watched": full_path in watched_paths,
                        "reason": reason,
                    }
                )

        # 3. Also scan /boot-config if present (Unraid flash — small, critical).
        boot_config = "/boot-config"
        if os.path.isdir(boot_config) and boot_config not in seen_paths:
            size, count = _quick_size(boot_config, max_files=2000)
            suggestions.append(
                {
                    "path": boot_config,
                    "label": "Flash Config",
                    "size_bytes": size,
                    "file_count": count,
                    "priority": "critical",
                    "recommended_excludes": [],
                    "already_watched": boot_config in watched_paths,
                    "reason": "Unraid USB flash configuration",
                }
            )

        # Sort suggestions: unwatched first, then by priority, then by name.
        priority_order = {"critical": 0, "recommended": 1, "optional": 2}
        suggestions.sort(
            key=lambda s: (
                s["already_watched"],
                priority_order.get(s["priority"], 3),
                s["path"],
            )
        )

        return directories, suggestions

    # Run the blocking filesystem scan in a thread so we don't block the event loop.
    directories, suggestions = await asyncio.to_thread(_build_scan_results)

    platform = detect_platform()
    return {
        "directories": directories,
        "suggestions": suggestions,
        "platform": platform.value,
    }
