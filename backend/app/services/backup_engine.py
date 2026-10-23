"""Restic wrapper for backup, restore, and snapshot management."""

import asyncio
import json
import logging
import os
import re
from typing import Any

from app.core.config import ArkiveConfig
from app.core.security import decrypt_value
from app.services.host_identity import resolve_hostname
from app.services.repo_paths import build_repo_path
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.backup_engine")

# Patterns that indicate transient/retryable errors
_TRANSIENT_PATTERNS = [
    "connection refused",
    "timeout",
    "network",
    "unreachable",
    "temporary failure",
    "dns",
    "i/o timeout",
    "connection reset",
    "broken pipe",
    "eof",
]

_NON_TRANSIENT_AUTH_PATTERNS = [
    "unable to authenticate",
    "authentication failed",
    "invalid password",
    "permission denied",
    "accessdenied",
    "signaturedoesnotmatch",
]

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 5  # seconds

# Wait for a competing restic lock (backup vs. prune/check/verify) instead of
# failing immediately with "repository is already locked".
RETRY_LOCK = "10m"
# restic backup exit code: snapshot created, but some source files could not
# be read (permission denied, vanished mid-backup).
RESTIC_EXIT_INCOMPLETE = 3
# Directories containing this file (Arkive's own repositories, restore scratch
# space) are skipped when they appear inside a watched directory.
EXCLUDE_MARKER = ".arkive-exclude"


# restic host names that are really Docker container IDs (the hostname inside
# a container), written before host names were resolved properly.
_CONTAINER_ID_HOST_RE = re.compile(r"^[0-9a-f]{12}$")


class ResticError(RuntimeError):
    """A restic command failed (as opposed to returning an empty result)."""


def _is_transient_error(stderr: str) -> bool:
    """Check if a restic error is likely transient and retryable."""
    lower = stderr.lower()
    if any(pattern in lower for pattern in _NON_TRANSIENT_AUTH_PATTERNS):
        return False
    return any(pattern in lower for pattern in _TRANSIENT_PATTERNS)


# Restic snapshot IDs are hex strings (8 or 64 chars), or "latest"
_SNAPSHOT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Path validation: block traversal sequences
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


def _validate_snapshot_id(snapshot_id: str) -> str:
    """Validate snapshot ID to prevent injection into restic commands."""
    if not snapshot_id or not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise ValueError(f"Invalid snapshot ID format: {snapshot_id!r}")
    return snapshot_id


def _validate_path(path: str) -> str:
    """Validate path argument to prevent traversal attacks."""
    if _PATH_TRAVERSAL_RE.search(path):
        raise ValueError(f"Path traversal not allowed: {path!r}")
    return path


def _snapshot_size_bytes(snapshot: dict[str, Any]) -> int:
    """Extract the best available size signal from a restic snapshot payload."""
    top_level = snapshot.get("size")
    if isinstance(top_level, int) and top_level >= 0:
        return top_level

    summary = snapshot.get("summary")
    if isinstance(summary, dict):
        for key in ("data_added_packed", "data_added", "total_bytes_processed"):
            value = summary.get(key)
            if isinstance(value, int) and value >= 0:
                return value

    return 0


class BackupEngine:
    """Wraps restic binary for backup operations."""

    def __init__(self, config: ArkiveConfig, docker_client=None):
        self.config = config
        self.docker_client = docker_client

    def _get_restic_env(self, password: str) -> dict[str, str]:
        """Get environment with restic password."""
        env = os.environ.copy()
        env["RESTIC_PASSWORD"] = password
        env["RESTIC_CACHE_DIR"] = "/cache"
        return env

    def _repo_path(self, target: dict) -> str:
        """Get restic repository path for a target."""
        return build_repo_path(target)

    async def _get_password(self) -> str:
        """Get restic encryption password from settings."""
        import aiosqlite

        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT value FROM settings WHERE key = 'encryption_password'")
            result = await row.fetchone()
            if result:
                return decrypt_value(result["value"])
        return ""

    async def _get_bandwidth_limit(self) -> str:
        """Read bandwidth_limit from settings (KiB/s integer or empty).

        Re-validates the stored value before returning to guard against
        corrupt or legacy DB values (e.g. "0", which restic treats as unlimited).
        Returns "" on DB error (table missing, bootstrap scenarios, etc.).
        """
        import aiosqlite

        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT value FROM settings WHERE key = 'bandwidth_limit'")
                result = await cursor.fetchone()
                if result and result["value"]:
                    val = result["value"]
                    if not re.match(r"^[1-9]\d*$", val):
                        logger.warning("bandwidth_limit '%s' failed re-validation — ignoring", val)
                        return ""
                    return val
        except Exception as e:
            logger.debug("Could not read bandwidth_limit, uploading unthrottled: %s", e)
        return ""

    async def _get_server_name(self) -> str:
        """Read optional server_name from settings."""
        import aiosqlite

        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT value FROM settings WHERE key = 'server_name'")
                result = await cursor.fetchone()
                return str(result["value"]).strip() if result and result["value"] else ""
        except Exception:
            return ""

    async def init_repo(self, target: dict) -> bool:
        """Initialize restic repository. Idempotent."""
        password = await self._get_password()
        if not password:
            logger.error("No encryption password configured")
            return False

        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        # Check if already initialized
        result = await run_command(
            ["restic", "-r", repo, "snapshots", "--json"],
            env=env,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("Repository already initialized: %s", repo)
            return True

        # Initialize
        result = await run_command(
            ["restic", "init", "-r", repo],
            env=env,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("Repository initialized: %s", repo)
            return True

        logger.error("Failed to init repo %s: %s", repo, result.stderr)
        return False

    async def backup(
        self,
        target: dict,
        paths: list[str],
        excludes: list[str] | None = None,
        tags: list[str] | None = None,
        cancel_check=None,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        """Run restic backup. Returns snapshot info."""
        password = await self._get_password()
        if not password:
            logger.error("No encryption password configured — cannot run backup")
            return {"status": "failed", "error": "No encryption password configured"}
        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        cmd = [
            "restic",
            "backup",
            "-r",
            repo,
            "--json",
            "--retry-lock",
            RETRY_LOCK,
            "--exclude-caches",
            "--exclude-if-present",
            EXCLUDE_MARKER,
        ]
        resolved_host = (hostname or "").strip() or await self.current_hostname()
        if resolved_host:
            cmd.extend(["--host", resolved_host])
        if tags:
            for tag in tags:
                cmd.extend(["--tag", tag])
        if excludes:
            for exc in excludes:
                cmd.extend(["--exclude", exc])
        bandwidth_limit = await self._get_bandwidth_limit()
        if bandwidth_limit:
            cmd.extend(["--limit-upload", bandwidth_limit])
        cmd.append("--")
        cmd.extend(paths)

        # Run with retry for transient network errors
        last_result = None
        for attempt in range(1, MAX_RETRIES + 1):
            last_result = await run_command(
                cmd,
                env=env,
                timeout=3600,
                cancel_check=cancel_check,
            )
            if last_result.returncode in (0, RESTIC_EXIT_INCOMPLETE, -2):
                break
            if attempt < MAX_RETRIES and _is_transient_error(last_result.stderr):
                wait_time = RETRY_BACKOFF_BASE * attempt
                logger.warning(
                    "Backup attempt %d/%d failed for %s (transient error), retrying in %ds: %s",
                    attempt,
                    MAX_RETRIES,
                    target.get("name"),
                    wait_time,
                    last_result.stderr[:200],
                )
                await asyncio.sleep(wait_time)
            else:
                break

        result = last_result
        if result.returncode == -2:
            logger.warning("Backup cancelled for %s", target.get("name"))
            return {"status": "cancelled", "error": result.stderr}
        if result.returncode not in (0, RESTIC_EXIT_INCOMPLETE):
            logger.error("Backup failed for %s: %s", target.get("name"), result.stderr)
            return {"status": "failed", "error": result.stderr}

        # Parse JSON output for snapshot summary and per-file errors
        snapshot_info = {}
        file_errors: list[str] = []
        for line in result.stdout.strip().split("\n"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("message_type") == "summary":
                snapshot_info = data
            elif data.get("message_type") == "error" and len(file_errors) < 20:
                err = data.get("error")
                message = err.get("message") if isinstance(err, dict) else err
                item = data.get("item") or ""
                file_errors.append(f"{item}: {message}" if item else str(message))

        if result.returncode == RESTIC_EXIT_INCOMPLETE:
            detail = "; ".join(file_errors) or result.stderr.strip()[:500]
            logger.warning("Backup for %s is incomplete — some files could not be read: %s", target.get("name"), detail)
            return {
                "status": "partial",
                "error": f"Some files could not be read: {detail}",
                "snapshot_id": snapshot_info.get("snapshot_id", ""),
                "total_bytes_processed": snapshot_info.get("total_bytes_processed", 0),
                "files_new": snapshot_info.get("files_new", 0),
                "files_changed": snapshot_info.get("files_changed", 0),
            }

        return {
            "status": "success",
            "snapshot_id": snapshot_info.get("snapshot_id", ""),
            "total_bytes_processed": snapshot_info.get("total_bytes_processed", 0),
            "files_new": snapshot_info.get("files_new", 0),
            "files_changed": snapshot_info.get("files_changed", 0),
        }

    async def forget(
        self, target: dict, keep_daily: int = 7, keep_weekly: int = 4, keep_monthly: int = 6, prune: bool = True
    ) -> dict:
        """Run restic forget with retention policy, optionally pruning unused data."""
        password = await self._get_password()
        if not password:
            logger.error("No encryption password configured — cannot run forget")
            return {"status": "failed", "output": "No encryption password configured"}

        # Validate retention values — prevent accidental deletion of all snapshots
        if keep_daily < 1 or keep_weekly < 1 or keep_monthly < 1:
            logger.error(
                "Invalid retention values: daily=%d weekly=%d monthly=%d — "
                "all values must be >= 1 to prevent data loss",
                keep_daily,
                keep_weekly,
                keep_monthly,
            )
            return {"status": "failed", "output": "Retention values must be >= 1 to prevent data loss"}

        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        cmd = [
            "restic",
            "forget",
            "-r",
            repo,
            "--retry-lock",
            RETRY_LOCK,
            "--keep-daily",
            str(keep_daily),
            "--keep-weekly",
            str(keep_weekly),
            "--keep-monthly",
            str(keep_monthly),
        ]
        if prune:
            cmd.append("--prune")
        cmd.append("--json")
        result = await run_command(cmd, env=env, timeout=3600 * 4 if prune else 900)

        return {
            "status": "success" if result.returncode == 0 else "failed",
            "output": result.stdout[:500],
            "error": result.stderr[:500] if result.returncode != 0 else None,
        }

    async def current_hostname(self) -> str:
        """Host name recorded in snapshots (stable across container re-creates).

        May query the Docker daemon, so it runs off the event loop.
        """
        return await asyncio.to_thread(
            resolve_hostname,
            settings={"server_name": await self._get_server_name()},
            docker_client=self.docker_client,
        )

    async def list_snapshots(self, target: dict) -> list[dict]:
        """List snapshots for a target; raises ResticError when restic fails,
        so callers can tell an unreachable repository from an empty one."""
        password = await self._get_password()
        if not password:
            raise ResticError("No encryption password configured")
        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        result = await run_command(
            ["restic", "snapshots", "-r", repo, "--json"],
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            raise ResticError(result.stderr.strip()[:500] or f"restic exited with {result.returncode}")

        try:
            snapshots = json.loads(result.stdout) or []
        except json.JSONDecodeError as exc:
            raise ResticError(f"Unreadable restic output: {exc}") from exc
        for snapshot in snapshots:
            if isinstance(snapshot, dict):
                snapshot["size"] = _snapshot_size_bytes(snapshot)
        return snapshots

    async def snapshots(self, target: dict) -> list[dict]:
        """List snapshots for a target ([] when the listing fails)."""
        try:
            return await self.list_snapshots(target)
        except ResticError:
            return []

    async def normalize_legacy_hostnames(self, target: dict, marker_path: str, owned_snapshot_ids: set[str]) -> dict:
        """Re-label this instance's snapshots recorded under a container ID.

        Older versions recorded the container hostname (its ID, which changes on
        every re-create), so each re-create started a new restic retention
        group and old groups were never thinned out. A snapshot is only touched
        when this instance's own run history recorded creating it
        (``owned_snapshot_ids``, full or short IDs) — a container-ID host name
        and Arkive's dump path don't prove ownership in a shared repository.
        The data is unchanged; only snapshot metadata is rewritten.
        """
        host = (await self.current_hostname()).strip()
        if not host or _CONTAINER_ID_HOST_RE.match(host):
            return {"status": "skipped", "rewritten": 0, "reason": "current host name is not stable"}
        try:
            snapshots = await self.list_snapshots(target)
        except ResticError as exc:
            return {"status": "failed", "rewritten": 0, "error": str(exc)}
        owned = {i for i in owned_snapshot_ids if i}

        def _is_owned(snap_id: str) -> bool:
            return snap_id in owned or snap_id[:8] in owned

        legacy = [
            snap["id"]
            for snap in snapshots
            if isinstance(snap, dict)
            and snap.get("id")
            and _CONTAINER_ID_HOST_RE.match(str(snap.get("hostname") or ""))
            and snap.get("hostname") != host
            and marker_path in (snap.get("paths") or [])
            and _is_owned(str(snap["id"]))
        ]
        if not legacy:
            return {"status": "success", "rewritten": 0}
        logger.info(
            "Re-labelling %d of this instance's snapshots from container-ID host names to %s; "
            "the next retention pass then applies one policy to the merged history",
            len(legacy),
            host,
        )

        password = await self._get_password()
        env = self._get_restic_env(password)
        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)
        result = await run_command(
            [
                "restic",
                "rewrite",
                "-r",
                self._repo_path(target),
                "--retry-lock",
                RETRY_LOCK,
                "--new-host",
                host,
                "--forget",
                "--",
                *legacy,
            ],
            env=env,
            timeout=3600,
        )
        if result.returncode != 0:
            return {"status": "failed", "rewritten": 0, "error": result.stderr.strip()[:500]}
        logger.info("Re-labelled %d snapshots from container-ID host names to %s", len(legacy), host)
        return {"status": "success", "rewritten": len(legacy), "host": host}

    async def restore(
        self, target: dict, snapshot_id: str, paths: list[str] | None = None, restore_to: str | None = None
    ) -> dict:
        """Restore files from a snapshot."""
        # Validate inputs to prevent injection
        snapshot_id = _validate_snapshot_id(snapshot_id)
        if not restore_to:
            return {
                "status": "failed",
                "error": "restore_to is required; refusing to restore into /",
            }

        _validate_path(restore_to)
        if paths:
            paths = [_validate_path(p) for p in paths]

        password = await self._get_password()
        if not password:
            return {"status": "failed", "error": "No encryption password configured"}
        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        cmd = ["restic", "restore", snapshot_id, "-r", repo]
        cmd.extend(["--target", restore_to])
        if paths:
            for p in paths:
                cmd.extend(["--include", p])

        result = await run_command(cmd, env=env, timeout=3600)
        return {
            "status": "success" if result.returncode == 0 else "failed",
            "output": result.stdout[:500],
            "error": result.stderr[:500] if result.returncode != 0 else None,
        }

    async def unlock(self, target: dict) -> bool:
        """Unlock stale restic locks."""
        password = await self._get_password()
        if not password:
            return False
        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        result = await run_command(
            ["restic", "unlock", "-r", repo],
            env=env,
            timeout=60,
        )
        return result.returncode == 0

    async def check(self, target: dict) -> dict:
        """Run restic check to verify repository integrity."""
        password = await self._get_password()
        if not password:
            return {"status": "failed", "error": "No encryption password configured"}
        repo = self._repo_path(target)
        env = self._get_restic_env(password)
        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)
        result = await run_command(
            ["restic", "check", "-r", repo, "--json", "--retry-lock", RETRY_LOCK],
            env=env,
            timeout=3600,
        )
        return {
            "status": "success" if result.returncode == 0 else "failed",
            "output": result.stdout[:1000] if result.stdout else "",
            "error": result.stderr[:500] if result.returncode != 0 and result.stderr else None,
        }

    async def ls(self, target: dict, snapshot_id: str, path: str = "/", strict: bool = False) -> list[dict]:
        """List files in a snapshot.

        Returns [] when listing fails, unless ``strict``: then ResticError is
        raised so an unreadable repository isn't mistaken for an empty folder.
        """
        # Validate inputs to prevent injection
        snapshot_id = _validate_snapshot_id(snapshot_id)
        _validate_path(path)

        password = await self._get_password()
        if not password:
            if strict:
                raise ResticError("No encryption password configured")
            return []
        repo = self._repo_path(target)
        env = self._get_restic_env(password)

        if target.get("type") != "local":
            env["RCLONE_CONFIG"] = str(self.config.rclone_config)

        result = await run_command(
            ["restic", "ls", snapshot_id, "-r", repo, "--json", path],
            env=env,
            timeout=120,
        )
        if result.returncode != 0:
            if strict:
                raise ResticError(result.stderr.strip()[:500] or f"restic ls exited with {result.returncode}")
            return []

        entries = []
        for line in result.stdout.strip().split("\n"):
            try:
                data = json.loads(line)
                if "name" in data:
                    entries.append(
                        {
                            "name": data.get("name", ""),
                            "type": "directory" if data.get("type") == "dir" else "file",
                            "size": data.get("size"),
                            "modified": data.get("mtime"),
                        }
                    )
            except json.JSONDecodeError:
                continue
        return entries
