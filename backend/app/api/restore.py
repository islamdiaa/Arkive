"""Restore API routes."""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.core.dependencies import get_backup_engine, get_config, get_db, get_restore_plan, require_auth
from app.core.security import decrypt_config
from app.models.restore import RestoreRequest
from app.services.orchestrator import _get_proc_start_time
from app.services.repo_paths import build_repo_path
from app.utils.mounts import read_only_mount_for

logger = logging.getLogger("arkive.restore")


class RestoreTestRequest(BaseModel):
    snapshot_id: str
    target_id: str
    path: str | None = None


# Restore-integrity tests pick a file at random; cap its size so a multi-GB
# dump or disk image can't fill the disk or stall the worker while hashing.
MAX_RESTORE_TEST_BYTES = 256 * 1024 * 1024


def _entry_size(entry: dict) -> int | None:
    size = entry.get("size")
    return size if isinstance(size, int) and size >= 0 else None


def _is_small_file(entry: dict) -> bool:
    if entry.get("type") != "file":
        return False
    size = _entry_size(entry)
    return size is None or size <= MAX_RESTORE_TEST_BYTES


async def _pick_restore_test_file(backup_engine, target: dict, snapshot_id: str) -> str | None:
    """Pick a random small file from the snapshot root or up to two levels below."""
    entries = await backup_engine.ls(target, snapshot_id, "/")
    files = [e for e in entries if _is_small_file(e)]
    if files:
        return "/" + random.choice(files)["name"]  # nosec B311
    dirs = [e for e in entries if e.get("type") in ("dir", "directory")]
    for d in random.sample(dirs, min(len(dirs), 5)):  # nosec B311
        subpath = "/" + d["name"]
        sub_entries = await backup_engine.ls(target, snapshot_id, subpath)
        sub_files = [e for e in sub_entries if _is_small_file(e)]
        if sub_files:
            return subpath + "/" + random.choice(sub_files)["name"]  # nosec B311
        sub_dirs = [e for e in sub_entries if e.get("type") in ("dir", "directory")]
        for sd in random.sample(sub_dirs, min(len(sub_dirs), 3)):  # nosec B311
            deep_path = subpath + "/" + sd["name"]
            deep_entries = await backup_engine.ls(target, snapshot_id, deep_path)
            deep_files = [e for e in deep_entries if _is_small_file(e)]
            if deep_files:
                return deep_path + "/" + random.choice(deep_files)["name"]  # nosec B311
    return None


def _locate_and_hash(tmpdir: str, expected_name: str) -> tuple[str | None, str, int]:
    """Find the restored file under tmpdir and SHA-256 it (blocking; run in a thread)."""
    restored_file = None
    first_file = None
    for dirpath, _, filenames in os.walk(tmpdir):
        for fname in filenames:
            candidate = os.path.join(dirpath, fname)
            if first_file is None:
                first_file = candidate
            if fname == expected_name:
                restored_file = candidate
                break
        if restored_file:
            break
    restored_file = restored_file or first_file
    if not restored_file:
        return None, "", 0
    sha256 = hashlib.sha256()
    file_size = 0
    with open(restored_file, "rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)
            file_size += len(chunk)
    return restored_file, sha256.hexdigest(), file_size


def _repo_path_for_target(target: dict) -> str:
    """Return the operator-facing repository path for restore documentation."""
    return build_repo_path(target)


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", snapshot_id):
        raise HTTPException(400, "Invalid snapshot ID format")
    return snapshot_id


def _acquire_restore_lock() -> None:
    """Acquire restore lock atomically. Raises HTTPException on conflict.

    Performs stale-lock recovery: if the lock file's PID no longer exists
    (or the proc start time doesn't match, indicating PID recycling), the
    stale lock is removed and acquisition is retried atomically.

    Uses a double-check pattern after O_EXCL creation to close the TOCTOU
    window with concurrent backup lock acquisition.
    """
    # Resolve paths dynamically so tests can override ARKIVE_CONFIG_DIR
    from pathlib import Path

    config_dir = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
    lock_file = config_dir / "backup.lock"
    restore_lock_file = config_dir / "restore.lock"

    if lock_file.exists():
        # Check if backup lock is stale
        try:
            lock_data = json.loads(lock_file.read_text())
            pid = lock_data.get("pid")
            if pid:
                stored_start = lock_data.get("proc_start_time")
                if not stored_start:
                    # Legacy lock without proc_start_time — treat as live (conservative)
                    raise HTTPException(409, "Cannot restore while a backup is running")
                current_start = _get_proc_start_time(int(pid))
                if current_start is not None and current_start == stored_start:
                    # Process genuinely alive — backup is running
                    raise HTTPException(409, "Cannot restore while a backup is running")
                # Process dead or PID recycled — stale lock
            lock_file.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError, ValueError):
            lock_file.unlink(missing_ok=True)
        except HTTPException:
            raise  # Re-raise the 409
        # Fall through — stale lock removed

    # Stale lock recovery — mirrors orchestrator._acquire_lock logic
    if restore_lock_file.exists():
        try:
            lock_data = json.loads(restore_lock_file.read_text())
            pid = lock_data.get("pid")
            if pid and os.path.exists(f"/proc/{pid}"):
                stored_start = lock_data.get("proc_start_time")
                if stored_start:
                    current_start = _get_proc_start_time(int(pid))
                    if current_start == stored_start:
                        raise HTTPException(409, "Another restore operation is already running")
                    # PID recycled — stale lock
                    logger.warning(
                        "Restore lock PID %s recycled (start %s→%s), removing stale lock",
                        pid,
                        stored_start,
                        current_start,
                    )
                else:
                    # Lock without proc_start_time — treat as live
                    raise HTTPException(409, "Another restore operation is already running")
            # Process dead — remove stale lock
            if pid:
                logger.warning("Removing stale restore lock from dead PID %s", pid)
            try:
                restore_lock_file.unlink()
            except OSError:
                pass
        except HTTPException:
            raise
        except Exception:
            # Corrupt lock file — remove and retry
            try:
                restore_lock_file.unlink()
            except OSError:
                pass

    try:
        restore_lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(restore_lock_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(
                fd,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "proc_start_time": _get_proc_start_time(os.getpid()) or "",
                        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                ).encode(),
            )
        finally:
            os.close(fd)
    except FileExistsError:
        raise HTTPException(409, "Another restore operation is already running")

    # Double-check: did a backup lock appear between our pre-check and O_EXCL create?
    if lock_file.exists():
        restore_lock_file.unlink(missing_ok=True)
        raise HTTPException(409, "Cannot restore while a backup is running")


def _release_restore_lock() -> None:
    """Release restore lock."""
    from pathlib import Path

    restore_lock_file = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config")) / "restore.lock"
    try:
        restore_lock_file.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Could not release restore lock %s: %s", restore_lock_file, e)


def cleanup_stale_restore_lock(config_dir: Path | None = None) -> bool:
    """Remove a stale restore.lock proactively on startup.

    Returns True when a stale or corrupt lock was removed.
    """
    config_root = config_dir or Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
    restore_lock_file = config_root / "restore.lock"
    if not restore_lock_file.exists():
        return False

    try:
        lock_data = json.loads(restore_lock_file.read_text())
        pid = lock_data.get("pid")
        stored_start = lock_data.get("proc_start_time")
        if pid and stored_start:
            current_start = _get_proc_start_time(int(pid))
            if current_start is not None and current_start == stored_start:
                return False
        restore_lock_file.unlink(missing_ok=True)
        return True
    except Exception:
        restore_lock_file.unlink(missing_ok=True)
        return True


router = APIRouter(prefix="/restore", tags=["restore"], dependencies=[Depends(require_auth)])


@router.get("/browse/{snapshot_id}")
async def browse_snapshot_alias(
    snapshot_id: str,
    path: str = "/",
    target_id: str = "",
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
):
    """Alias for snapshot browsing — delegates to the snapshots browse logic.

    GET /restore/browse/{snapshot_id}?path=/&target_id=...
    """
    _validate_snapshot_id(snapshot_id)

    cursor = await db.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
    snap = await cursor.fetchone()
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    snap = dict(snap)

    config = get_config()
    tid = target_id or snap["target_id"]
    tc = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (tid,))
    target = await tc.fetchone()
    if not target:
        raise HTTPException(404, "Target not found")
    target = dict(target)
    target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))

    try:
        entries = await backup_engine.ls(target, snap["full_id"], path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": path, "entries": entries, "snapshot_id": snapshot_id}


@router.post("")
async def restore_files(
    body: RestoreRequest,
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
):
    """Restore files from a snapshot.

    Supports dry_run mode: when dry_run=true, lists files that would be
    restored without actually writing anything to disk.
    """
    # Get target
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (body.target,))
    target = await cursor.fetchone()
    if not target:
        raise HTTPException(404, "Target not found")
    target = dict(target)
    config = get_config()
    target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))

    restore_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()

    if body.dry_run:
        # Dry run: list files that would be restored without writing
        try:
            entries = await backup_engine.ls(
                target,
                body.snapshot_id,
                body.paths[0] if body.paths else "/",
            )
            return {
                "restore_id": restore_id,
                "status": "dry_run",
                "snapshot_id": body.snapshot_id,
                "message": f"Dry run: {len(entries)} entries would be restored",
                "entries": entries,
                "dry_run": True,
            }
        except Exception as e:
            logger.warning("Dry run failed: %s", e)
            return {
                "restore_id": restore_id,
                "status": "failed",
                "snapshot_id": body.snapshot_id,
                "message": f"Dry run failed: {e}",
                "dry_run": True,
            }

    restore_to = body.restore_to or str(Path(config.restore_dir) / f"restore-{restore_id}")

    # Arkive is meant to run with the data it backs up mounted read-only; a
    # restore into such a mount would fail file-by-file deep inside restic.
    destinations = [restore_to] + [os.path.join(restore_to, p.lstrip("/")) for p in (body.paths or []) if p.strip("/")]
    for destination in destinations:
        read_only_mount = read_only_mount_for(destination)
        if read_only_mount:
            raise HTTPException(
                400,
                f"Cannot restore to {destination}: {read_only_mount} is mounted read-only in the "
                f"Arkive container. Restore to {config.restore_dir} (the default) and copy the files "
                f"into place, or mount {read_only_mount} read-write.",
            )

    # Take the lock before recording anything: a 409 (backup running / another
    # restore) must not leave a restore_runs row stuck in 'running'.
    _acquire_restore_lock()
    try:
        if not body.restore_to:
            await asyncio.to_thread(Path(restore_to).mkdir, parents=True, exist_ok=True)

        await db.execute(
            """INSERT INTO restore_runs
               (id, target_id, snapshot_id, paths, restore_to, status, started_at)
               VALUES (?, ?, ?, ?, ?, 'running', ?)""",
            (
                restore_id,
                body.target,
                body.snapshot_id,
                json.dumps(body.paths if body.paths else []),
                restore_to,
                started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        await db.execute(
            """INSERT INTO activity_log (type, action, message, details, severity)
               VALUES ('restore', 'run_started', ?, ?, 'info')""",
            (
                "Manual restore started",
                json.dumps(
                    {
                        "restore_id": restore_id,
                        "target_id": body.target,
                        "snapshot_id": body.snapshot_id,
                        "restore_to": restore_to,
                    }
                ),
            ),
        )
        await db.commit()

        try:
            result = await backup_engine.restore(
                target=target,
                snapshot_id=body.snapshot_id,
                paths=body.paths if body.paths else None,
                restore_to=restore_to,
            )
        except BaseException as exc:
            duration = int(time.monotonic() - started_monotonic)
            interrupted = isinstance(exc, asyncio.CancelledError)
            message = "Restore interrupted" if interrupted else str(exc)
            await db.execute(
                """UPDATE restore_runs
                   SET status = 'failed', completed_at = ?, duration_seconds = ?, error_message = ?
                   WHERE id = ?""",
                (
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    max(duration, 0),
                    message,
                    restore_id,
                ),
            )
            await db.execute(
                """INSERT INTO activity_log (type, action, message, details, severity)
                   VALUES ('restore', 'completed', ?, ?, 'warning')""",
                (
                    f"Restore failed: {message}",
                    json.dumps({"restore_id": restore_id, "status": "failed"}),
                ),
            )
            await db.commit()
            raise
    finally:
        _release_restore_lock()

    duration = int(time.monotonic() - started_monotonic)
    error_message = None
    result_status = result.get("status", "unknown")
    if result_status != "success":
        error_message = result.get("error") or result.get("output") or "Restore failed"
    await db.execute(
        """UPDATE restore_runs
           SET status = ?, completed_at = ?, duration_seconds = ?, error_message = ?
           WHERE id = ?""",
        (
            result_status,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            max(duration, 0),
            error_message,
            restore_id,
        ),
    )
    severity = "info" if result_status == "success" else "warning"
    await db.execute(
        """INSERT INTO activity_log (type, action, message, details, severity)
           VALUES ('restore', 'completed', ?, ?, ?)""",
        (
            f"Restore {result_status}",
            json.dumps({"restore_id": restore_id, "status": result_status}),
            severity,
        ),
    )
    await db.commit()

    return {
        "restore_id": restore_id,
        "status": result_status,
        "snapshot_id": body.snapshot_id,
        "restore_to": restore_to,
        "message": result.get("output") or result.get("error", ""),
    }


@router.post("/test")
async def test_restore_integrity(
    body: RestoreTestRequest,
    db: aiosqlite.Connection = Depends(get_db),
    backup_engine=Depends(get_backup_engine),
):
    """Test restore integrity by restoring a file and computing its SHA-256 hash.

    Accepts: {"snapshot_id": "abc", "target_id": "xyz", "path": "/optional/file"}
    Returns: {"status": "success", "file": path, "sha256": hash, "size_bytes": N, "duration_ms": N}
    """
    start = time.monotonic()

    snapshot_id = _validate_snapshot_id(body.snapshot_id)
    target_id = body.target_id

    # Look up target from DB
    cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (target_id,))
    target = await cursor.fetchone()
    if not target:
        raise HTTPException(404, "Target not found")
    target = dict(target)
    config = get_config()
    target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))

    file_path = body.path
    tmpdir = None
    try:
        if not file_path:
            try:
                file_path = await _pick_restore_test_file(backup_engine, target, snapshot_id)
            except Exception as e:
                logger.exception("Failed to list snapshot files: %s", e)
                return {"status": "failed", "error": "Failed to list snapshot files"}
            if not file_path:
                return {
                    "status": "failed",
                    "error": (
                        f"No files found in snapshot (restore tests only use files up to "
                        f"{MAX_RESTORE_TEST_BYTES // (1024 * 1024)} MB)"
                    ),
                }
        else:
            # Refuse oversized user-selected files when the listing reports a size.
            parent = os.path.dirname(file_path.rstrip("/")) or "/"
            try:
                siblings = await backup_engine.ls(target, snapshot_id, parent)
            except Exception:
                siblings = []
            match = next(
                (e for e in siblings if e.get("name") == os.path.basename(file_path.rstrip("/"))),
                None,
            )
            size = _entry_size(match) if match else None
            if size is not None and size > MAX_RESTORE_TEST_BYTES:
                return {
                    "status": "failed",
                    "error": (
                        f"File is too large for a restore test ({size} bytes; limit {MAX_RESTORE_TEST_BYTES} bytes)"
                    ),
                }

        # Restore into the persistent restores/ dir (not the container's /tmp,
        # which lives in docker.img on Unraid).
        restore_root = Path(config.restore_dir)
        await asyncio.to_thread(restore_root.mkdir, parents=True, exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix="arkive-test-", dir=str(restore_root))
        try:
            result = await backup_engine.restore(
                target=target,
                snapshot_id=snapshot_id,
                paths=[file_path],
                restore_to=tmpdir,
            )
        except Exception as e:
            logger.exception("Restore operation failed: %s", e)
            return {"status": "failed", "error": "Internal error during restore operation"}

        if result.get("status") != "success":
            return {
                "status": "failed",
                "error": result.get("error") or result.get("output", "Restore returned non-success status"),
            }

        restored_file, digest, file_size = await asyncio.to_thread(
            _locate_and_hash, tmpdir, os.path.basename(file_path)
        )
        if not restored_file:
            return {"status": "failed", "error": "Restored file not found in temp directory"}

        duration_ms = round((time.monotonic() - start) * 1000)

        return {
            "status": "success",
            "file": file_path,
            "sha256": digest,
            "size_bytes": file_size,
            "duration_ms": duration_ms,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Restore integrity test failed: %s", e)
        return {"status": "failed", "error": "Internal error during restore operation"}
    finally:
        if tmpdir and os.path.exists(tmpdir):
            await asyncio.to_thread(shutil.rmtree, tmpdir, True)


@router.get("/plan")
async def get_restore_plan_markdown(
    restore_plan=Depends(get_restore_plan),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Return the restore plan as a Markdown document."""
    import socket
    from datetime import datetime

    tc = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
    targets = [dict(t) for t in await tc.fetchall()]
    config = get_config()
    for target in targets:
        target["config"] = decrypt_config(target.get("config", "{}"), str(config.config_dir))
        target["repo_path"] = _repo_path_for_target(target)
    cc = await db.execute("SELECT * FROM discovered_containers ORDER BY priority")
    containers = [dict(c) for c in await cc.fetchall()]
    dc = await db.execute("SELECT * FROM watched_directories WHERE enabled = 1")
    directories = [dict(d) for d in await dc.fetchall()]

    hostname = socket.gethostname()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Arkive Disaster Recovery Plan",
        "",
        f"**Server:** {hostname}  ",
        f"**Generated:** {generated_at}  ",
        "",
        "> **CRITICAL:** Store this document securely. "
        "It contains the information needed to restore your entire server from scratch.",
        "",
        "## 1. Storage Targets",
        "",
    ]
    if targets:
        lines.append("| Name | Type | Enabled | Repo |")
        lines.append("|------|------|---------|------|")
        for t in targets:
            lines.append(f"| {t.get('name', '')} | {t.get('type', '')} | yes | `{t.get('repo_path', '')}` |")
    else:
        lines.append("_No storage targets configured._")

    lines += ["", "## 2. Containers", ""]
    if containers:
        lines.append("| Name | Image | Status | Profile |")
        lines.append("|------|-------|--------|---------|")
        for c in containers:
            lines.append(
                f"| {c.get('name', '')} | {c.get('image', '')} | {c.get('status', '')} | {c.get('profile', '')} |"
            )
    else:
        lines.append("_No containers discovered._")

    lines += ["", "## 3. Watched Directories", ""]
    if directories:
        lines.append("| Path | Enabled |")
        lines.append("|------|---------|")
        for d in directories:
            lines.append(f"| {d.get('path', '')} | yes |")
    else:
        lines.append("_No watched directories configured._")

    lines += ["", "## 4. Recovery Steps", ""]
    lines += [
        "1. Install restic on the recovery machine.",
        "2. Configure repository credentials from your secure password store.",
    ]
    if targets:
        lines += ["3. Use the following concrete commands for each configured target:", ""]
        for target in targets:
            repo = target.get("repo_path", "REPO")
            lines += [
                f"### {target.get('name', target.get('id', 'Target'))}",
                "",
                f"- List snapshots: `restic -r {repo} snapshots`",
                f"- Restore latest: `restic -r {repo} restore latest --target /restore-staging`",
                f"- Restore specific snapshot: `restic -r {repo} restore <snapshot-id> --target /restore-staging`",
                "",
            ]
        lines += [
            "4. Restore the required directories into staging.",
            "5. Restart containers in dependency order.",
        ]
    else:
        lines += [
            "3. Run `restic snapshots` against the target repo listed above to list available restore points.",
            "4. Run `restic restore <snapshot-id> --target /restore-staging` against that same repo to restore files into a staging directory.",
            "5. Restart containers in dependency order.",
        ]
    lines += ["", "---", f"_Generated by Arkive on {generated_at}_"]

    markdown_text = "\n".join(lines)
    return Response(content=markdown_text, media_type="text/markdown")


@router.get("/plan/pdf")
async def download_restore_plan(restore_plan=Depends(get_restore_plan)):
    """Generate and download the restore plan PDF."""
    pdf_path = await restore_plan.generate()
    if pdf_path.endswith(".pdf"):
        return FileResponse(pdf_path, media_type="application/pdf", filename="arkive-restore-plan.pdf")
    return FileResponse(pdf_path, media_type="text/html", filename="arkive-restore-plan.html")


@router.get("/plan/preview")
async def preview_restore_plan(
    restore_plan=Depends(get_restore_plan),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Preview restore plan data without generating PDF."""
    import socket
    from datetime import datetime

    tc = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
    targets = [dict(t) for t in await tc.fetchall()]
    cc = await db.execute("SELECT * FROM discovered_containers")
    containers = [dict(c) for c in await cc.fetchall()]

    return {
        "hostname": socket.gethostname(),
        "generated_at": datetime.now(UTC).isoformat(),
        "targets": len(targets),
        "containers": len(containers),
    }
