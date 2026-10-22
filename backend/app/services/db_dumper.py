"""Database dump engine — safe dumps for Postgres, SQLite, MariaDB, MongoDB, Redis."""

import asyncio
import gzip
import hashlib
import io
import logging
import os
import re
import shlex
import shutil
import tarfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import docker

from app.core.config import ArkiveConfig
from app.models.discovery import DiscoveredDatabase
from app.utils.subprocess_runner import run_command
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.db_dumper")

# Trailers written by the dump tools as their very last output. A dump that
# lacks its trailer was cut short (connection drop, OOM kill, timeout).
PG_DUMP_TRAILER = b"PostgreSQL database dump complete"
MYSQL_DUMP_TRAILER = b"-- Dump completed"
_TAIL_BYTES = 4096

# SQLite integrity-check errors that mean "this stock sqlite3 binary can't
# evaluate the schema" (app-specific collations/extensions such as Plex's
# icu_root), not that the database is corrupt.
_SQLITE_UNVERIFIABLE_MARKERS = (
    "no such collation sequence",
    "no such module",
    "no such function",
    "no such tokenizer",
)

# Files in the dump directory that are not per-database dumps and are managed
# elsewhere (flash backup retention, Arkive self-backup).
_NON_DUMP_PREFIXES = ("flash_",)
_NON_DUMP_FILES = {"arkive_self.db"}
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_DUMP_TS_PATTERN = re.compile(r"^(.+?)_(\d{8}_\d{6})\.")


@dataclass
class StreamResult:
    exit_code: int | None
    bytes_written: int
    stderr: str
    tail: bytes


def cleanup_dump_dir(dump_dir: Path, keep_last: int = 1, max_age_days: int | None = 7) -> int:
    """Prune the database dump directory.

    Keeps the newest ``keep_last`` dumps per container/database and removes
    any dump older than ``max_age_days`` (dumps of containers that no longer
    exist would otherwise be re-uploaded in every snapshot forever). Orphaned
    SQLite ``-wal``/``-shm``/``-journal`` sidecars are removed with their base
    file. Flash archives and the Arkive self-backup are left alone.

    Returns the number of files removed.
    """
    if not dump_dir.is_dir():
        return 0
    keep_last = max(keep_last, 1)
    cutoff = time.time() - max_age_days * 86400 if max_age_days else None

    groups: dict[str, list[Path]] = {}
    sidecars: list[Path] = []
    for path in dump_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name in _NON_DUMP_FILES or name.startswith(_NON_DUMP_PREFIXES):
            continue
        if name.endswith(_SQLITE_SIDECAR_SUFFIXES):
            sidecars.append(path)
            continue
        match = _DUMP_TS_PATTERN.match(name)
        prefix = match.group(1) if match else name
        groups.setdefault(prefix, []).append(path)

    to_remove: list[Path] = []
    for files in groups.values():
        # Timestamps in the name sort chronologically; newest first.
        files.sort(key=lambda p: p.name, reverse=True)
        for index, path in enumerate(files):
            if index >= keep_last:
                to_remove.append(path)
            elif cutoff is not None:
                try:
                    if path.stat().st_mtime < cutoff:
                        to_remove.append(path)
                except OSError:
                    continue

    removed = 0
    removed_names = {p.name for p in to_remove}
    for path in to_remove:
        try:
            path.unlink()
            removed += 1
            logger.info("Removed old dump: %s", path)
        except OSError as e:
            logger.warning("Failed to remove old dump %s: %s", path, e)

    for sidecar in sidecars:
        base = sidecar.name
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base in removed_names or not (dump_dir / base).exists():
            try:
                sidecar.unlink()
                removed += 1
            except OSError as e:
                logger.warning("Failed to remove dump sidecar %s: %s", sidecar, e)

    if removed:
        logger.info("Dump cleanup: removed %d old dump files", removed)
    return removed


@dataclass
class DumpResult:
    container_name: str
    db_type: str
    db_name: str
    dump_path: str
    dump_size_bytes: int
    integrity_check: str  # ok, failed, skipped
    status: str  # success, failed
    error: str | None = None
    duration_seconds: float = 0.0


class DBDumper:
    """Dumps databases using native tools."""

    def __init__(self, docker_client, config: ArkiveConfig):
        self.docker = docker_client
        self.config = config
        self.dump_dir = config.dump_dir
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        # (container, db_name) pairs shared by several distinct databases,
        # e.g. two SQLite files both named credentials.db in one container.
        self._name_collisions: set[tuple[str, str]] = set()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def _safe(value: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_.\-]', '_', value)

    def _dump_path(self, db: DiscoveredDatabase, ts: str, ext: str, db_label: str | None = None) -> str:
        """Build a unique dump file path for a database."""
        label = self._safe(db_label or db.db_name)
        if (db.container_name, db.db_name) in self._name_collisions and db.host_path:
            # Same basename in one container — disambiguate by source path so
            # the dumps don't overwrite each other within the same second.
            label = f"{label}-{hashlib.sha1(db.host_path.encode()).hexdigest()[:8]}"  # nosec B324
        return str(self.dump_dir / f"{self._safe(db.container_name)}_{label}_{ts}{ext}")

    @staticmethod
    def dedupe(databases: list[DiscoveredDatabase]) -> list[DiscoveredDatabase]:
        """Drop exact duplicates (a companion DB discovered via both the app
        container's profile and the DB container itself)."""
        seen: set[tuple[str, str, str, str | None]] = set()
        unique = []
        for db in databases:
            key = (db.container_name, db.db_type, db.db_name, db.host_path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(db)
        return unique

    def _exec_stream_to_gzip(
        self, container, cmd: list[str], dump_path: str, environment: dict | None = None,
    ) -> StreamResult:
        """Run ``cmd`` in ``container`` and stream stdout into a gzip file.

        docker-py's ``exec_run(stream=True)`` always reports ``exit_code=None``,
        which hid failed dumps. With a real Docker API client we use the
        low-level exec API so the exit code can be inspected after the stream
        ends; other clients fall back to ``exec_run``.
        """
        api = getattr(getattr(container, "client", None), "api", None)
        exec_id = None
        if isinstance(api, docker.APIClient):
            exec_id = api.exec_create(
                container.id, cmd, stdout=True, stderr=True, environment=environment or None,
            )["Id"]
            output = api.exec_start(exec_id, stream=True, demux=True)
            exit_code = None
        else:
            kwargs = {"environment": environment} if environment else {}
            exit_code, output = container.exec_run(cmd, demux=True, stream=True, **kwargs)

        bytes_written = 0
        tail = b""
        stderr_chunks: list[bytes] = []
        with gzip.open(dump_path, "wb") as f:
            for chunk in output:
                if isinstance(chunk, tuple):
                    stdout_data, stderr_data = chunk
                else:
                    stdout_data, stderr_data = chunk, None
                if stdout_data:
                    f.write(stdout_data)
                    bytes_written += len(stdout_data)
                    tail = (tail + stdout_data)[-_TAIL_BYTES:]
                if stderr_data:
                    stderr_chunks.append(stderr_data)

        if exec_id is not None:
            try:
                exit_code = api.exec_inspect(exec_id).get("ExitCode")
            except Exception as e:
                logger.warning("Could not read exit code for %s: %s", cmd[0], e)

        return StreamResult(
            exit_code=exit_code,
            bytes_written=bytes_written,
            stderr=b"".join(stderr_chunks).decode(errors="replace").strip(),
            tail=tail,
        )

    def _streamed_dump_result(
        self, db: DiscoveredDatabase, db_type: str, tool: str, dump_path: str,
        stream: StreamResult, trailer: bytes | None,
    ) -> DumpResult:
        """Turn a streamed dump into a DumpResult, detecting failure and truncation."""
        def failed(error: str) -> DumpResult:
            return DumpResult(
                container_name=db.container_name, db_type=db_type,
                db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                integrity_check="failed" if stream.bytes_written else "skipped",
                status="failed", error=redact_credentials(error),
            )

        if stream.exit_code is not None and stream.exit_code != 0:
            logger.error("%s for %s/%s failed (exit %s): %s", tool, db.container_name, db.db_name,
                         stream.exit_code, redact_credentials(stream.stderr[:500]))
            return failed(f"{tool} exited with code {stream.exit_code}: {stream.stderr[:500] or 'no output'}")

        if stream.bytes_written == 0:
            return failed(stream.stderr[:500] or "Empty dump file")

        integrity = "skipped"
        if trailer is not None:
            if trailer in stream.tail:
                integrity = "ok"
            else:
                logger.error("%s output for %s/%s is truncated (no completion trailer)",
                             tool, db.container_name, db.db_name)
                return failed(f"{tool} output is incomplete (missing completion trailer): "
                              f"{stream.stderr[:300] or 'dump was cut short'}")

        if stream.bytes_written < 100:
            logger.warning("%s for %s/%s produced suspiciously small output (%d bytes)",
                           tool, db.container_name, db.db_name, stream.bytes_written)
        if stream.stderr and (db_type != "mongodb" or "error" in stream.stderr.lower()):
            logger.warning("%s stderr for %s/%s: %s", tool, db.container_name, db.db_name,
                           redact_credentials(stream.stderr[:500]))

        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
        return DumpResult(
            container_name=db.container_name, db_type=db_type,
            db_name=db.db_name, dump_path=dump_path, dump_size_bytes=size,
            integrity_check=integrity, status="success",
        )

    async def dump_all(self, databases: list[DiscoveredDatabase]) -> list[DumpResult]:
        """Dump all discovered databases. Continue on failure."""
        databases = self.dedupe(databases)
        name_counts = Counter((db.container_name, db.db_name) for db in databases)
        self._name_collisions = {key for key, count in name_counts.items() if count > 1}
        results = []
        for db in databases:
            try:
                result = await self._dump_one(db)
                if result.status != "success":
                    # A failed/truncated dump must not shadow the previous
                    # good dump (cleanup keeps only the newest file).
                    self._remove_dump_file(result.dump_path)
                results.append(result)
            except Exception as e:
                error_str = str(e)
                redacted_error = redact_credentials(error_str)
                logger.error("Failed to dump %s/%s: %s", db.container_name, db.db_name, redacted_error)
                results.append(DumpResult(
                    container_name=db.container_name,
                    db_type=db.db_type,
                    db_name=db.db_name,
                    dump_path="",
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=redacted_error,
                ))
        return results

    @staticmethod
    def _remove_dump_file(path: str) -> None:
        if not path:
            return
        for candidate in (path, *(path + suffix for suffix in _SQLITE_SIDECAR_SUFFIXES)):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Could not remove failed dump %s: %s", candidate, e)

    async def _dump_one(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump a single database."""
        start = time.monotonic()
        if db.db_type == "postgres":
            # docker-py exec streams are blocking; offload to thread.
            result = await asyncio.to_thread(self._dump_postgres_blocking, db)
        elif db.db_type == "sqlite":
            result = await self._dump_sqlite(db)
        elif db.db_type in ("mariadb", "mysql"):
            # docker-py exec streams are blocking; offload to thread.
            result = await asyncio.to_thread(self._dump_mariadb_blocking, db)
        elif db.db_type == "mongodb":
            # docker-py exec streams are blocking; offload to thread.
            result = await asyncio.to_thread(self._dump_mongodb_blocking, db)
        elif db.db_type == "redis":
            # docker-py exec runs are blocking; offload to thread.
            result = await asyncio.to_thread(self._dump_redis_blocking, db)
        else:
            return DumpResult(
                container_name=db.container_name, db_type=db.db_type,
                db_name=db.db_name, dump_path="", dump_size_bytes=0,
                integrity_check="skipped", status="failed",
                error=f"Unsupported database type: {db.db_type}",
            )
        result.duration_seconds = round(time.monotonic() - start, 2)
        return result

    def _get_container_env(self, container) -> dict[str, str]:
        """Extract environment variables from a container as a dict."""
        env_list = container.attrs.get("Config", {}).get("Env", [])
        env_dict: dict[str, str] = {}
        for item in env_list:
            if "=" in item:
                k, _, v = item.partition("=")
                env_dict[k] = v
        return env_dict

    @staticmethod
    def _sanitize_identifier(value: str) -> str:
        """Sanitize a database identifier (username, db name) to prevent injection.

        Only allows alphanumeric characters, hyphens, underscores, and dots.
        Raises ValueError if the value contains disallowed characters.
        """
        if not value or not re.match(r'^[a-zA-Z0-9_.\-]+$', value):
            raise ValueError("Invalid identifier: contains disallowed characters")
        return value

    @staticmethod
    def _copy_file_from_container_archive(container, container_path: str, dump_path: str) -> tuple[bool, str | None]:
        """Copy a file out of a container via the Docker archive API."""
        try:
            stream, _stat = container.get_archive(container_path)
        except Exception as exc:
            return False, str(exc)

        buffer = io.BytesIO()
        for chunk in stream:
            if chunk:
                buffer.write(chunk)
        buffer.seek(0)

        try:
            with tarfile.open(fileobj=buffer, mode="r:*") as archive:
                member = next((item for item in archive.getmembers() if item.isfile()), None)
                if member is None:
                    return False, f"archive for {container_path} did not contain a file"
                extracted = archive.extractfile(member)
                if extracted is None:
                    return False, f"failed to extract {container_path} from container archive"
                with open(dump_path, "wb") as handle:
                    shutil.copyfileobj(extracted, handle)
        except Exception as exc:
            return False, str(exc)

        if not os.path.exists(dump_path) or os.path.getsize(dump_path) == 0:
            return False, f"copied {container_path} but resulting dump was empty"

        return True, None

    def _dump_postgres_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump Postgres via docker exec with streaming."""
        dump_path = self._dump_path(db, self._timestamp(), ".sql.gz")

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)
            pg_user = (
                env.get("POSTGRES_USER")
                or env.get("DB_USERNAME")
                or env.get("DB_USER")
                or "postgres"
            )
            # Sanitize identifiers to prevent command injection
            pg_user = self._sanitize_identifier(pg_user)
            db_name = self._sanitize_identifier(db.db_name)
            # Produce a self-contained logical dump that can be restored into a
            # fresh instance without recreating roles by hand.
            exec_cmd = [
                "pg_dump",
                "--clean",
                "--create",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "-U",
                pg_user,
                "-d",
                db_name,
            ]
            stream = self._exec_stream_to_gzip(container, exec_cmd, dump_path)
            return self._streamed_dump_result(db, "postgres", "pg_dump", dump_path, stream, PG_DUMP_TRAILER)
        except Exception as e:
            return DumpResult(
                container_name=db.container_name, db_type="postgres",
                db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                integrity_check="skipped", status="failed", error=redact_credentials(str(e)),
            )

    async def _dump_sqlite(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump SQLite using HOST sqlite3 binary on bind-mounted path."""
        dump_path = self._dump_path(db, self._timestamp(), ".sqlite3")

        if not db.host_path:
            return DumpResult(
                container_name=db.container_name, db_type="sqlite",
                db_name=db.db_name, dump_path="", dump_size_bytes=0,
                integrity_check="skipped", status="failed",
                error="No host path available for SQLite backup",
            )

        # Validate host_path is absolute and does not contain traversal
        host_path = os.path.normpath(db.host_path)
        if not os.path.isabs(host_path) or ".." in db.host_path:
            return DumpResult(
                container_name=db.container_name, db_type="sqlite",
                db_name=db.db_name, dump_path="", dump_size_bytes=0,
                integrity_check="skipped", status="failed",
                error="Invalid host path: must be absolute with no traversal",
            )

        # Use host sqlite3 binary — NEVER cp
        # shlex.quote() prevents injection via dump_path containing spaces/special chars
        async def _run_backup(source: str):
            return await run_command(["sqlite3", source, f".backup {shlex.quote(dump_path)}"])

        result = await _run_backup(host_path)
        backup_error = (result.stderr or result.stdout or "").strip()

        # Some live SQLite files mounted read-only into Arkive can fail to open
        # unless we tell SQLite to treat the source as immutable.
        if ("unable to open database file" in backup_error.lower()) or result.returncode != 0:
            immutable_source = f"file:{host_path}?mode=ro&immutable=1"
            retry = await _run_backup(immutable_source)
            retry_error = (retry.stderr or retry.stdout or "").strip()
            if retry.returncode == 0 and os.path.exists(dump_path) and os.path.getsize(dump_path) > 0:
                result = retry
            else:
                combined = retry_error or backup_error
                return DumpResult(
                    container_name=db.container_name, db_type="sqlite",
                    db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                    integrity_check="failed", status="failed", error=combined,
                )

        if result.returncode != 0:
            return DumpResult(
                container_name=db.container_name, db_type="sqlite",
                db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                integrity_check="failed", status="failed", error=result.stderr,
            )

        # .backup preserves the source's WAL journal mode; switch the copy to a
        # rollback journal so it is a single self-contained file and opening it
        # (integrity check, restore) doesn't leave -wal/-shm sidecars behind.
        await run_command(["sqlite3", dump_path, "PRAGMA journal_mode=DELETE;"])

        # Integrity check
        integrity = "skipped"
        check_result = await run_command(["sqlite3", dump_path, "PRAGMA integrity_check"])
        check_output = f"{check_result.stdout}\n{check_result.stderr}".lower()
        if any(marker in check_output for marker in _SQLITE_UNVERIFIABLE_MARKERS):
            # App-specific collation/extension (e.g. Plex icu_root) — the
            # stock sqlite3 binary cannot evaluate it, which is not corruption.
            integrity = "skipped"
        elif check_result.returncode == 0 and check_result.stdout.strip().lower() == "ok":
            integrity = "ok"
        else:
            integrity = "failed"
            logger.warning("SQLite integrity check failed for %s/%s: %s", db.container_name, db.db_name,
                           (check_result.stdout or check_result.stderr)[:300].strip())

        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
        return DumpResult(
            container_name=db.container_name, db_type="sqlite",
            db_name=db.db_name, dump_path=dump_path, dump_size_bytes=size,
            integrity_check=integrity, status="success" if size > 0 else "failed",
        )

    def _dump_mariadb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MariaDB/MySQL via docker exec with streaming."""
        dump_path = self._dump_path(db, self._timestamp(), ".sql.gz")

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)
            image_tags = [t.lower() for t in getattr(getattr(container, "image", None), "tags", [])]
            dump_binary = "mariadb-dump" if any("mariadb" in tag for tag in image_tags) else "mysqldump"
            # Select consistent user/password pairs:
            # 1. root user with root password
            # 2. app user with app password
            # 3. fallback to root with no password
            root_password = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD")
            app_user = env.get("MYSQL_USER") or env.get("MARIADB_USER")
            app_password = env.get("MYSQL_PASSWORD") or env.get("MARIADB_PASSWORD")

            if root_password:
                mysql_user = "root"
                mysql_password = root_password
            elif app_user and app_password:
                mysql_user = app_user
                mysql_password = app_password
            else:
                mysql_user = "root"
                mysql_password = ""
            # Sanitize identifiers to prevent command injection
            mysql_user = self._sanitize_identifier(mysql_user)
            db_name = self._sanitize_identifier(db.db_name)
            # Use list form to avoid shell interpretation;
            # pass password via environment variable to avoid it appearing in process list
            exec_env = {}
            # --single-transaction gives a consistent InnoDB snapshot without
            # locking tables for the whole dump.
            exec_cmd = [dump_binary, "-u", mysql_user, "--single-transaction"]
            if mysql_password:
                exec_env["MYSQL_PWD"] = mysql_password
            exec_cmd.extend(["--databases", db_name])
            stream = self._exec_stream_to_gzip(container, exec_cmd, dump_path, environment=exec_env)
            return self._streamed_dump_result(db, "mariadb", dump_binary, dump_path, stream, MYSQL_DUMP_TRAILER)
        except Exception as e:
            return DumpResult(
                container_name=db.container_name, db_type="mariadb",
                db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                integrity_check="skipped", status="failed", error=redact_credentials(str(e)),
            )

    def _dump_mongodb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MongoDB via docker exec with authentication support."""
        dump_path = self._dump_path(db, self._timestamp(), ".archive.gz")

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)

            # Build mongodump command with authentication if credentials are available
            exec_cmd = ["mongodump", "--archive"]
            mongo_user = env.get("MONGO_INITDB_ROOT_USERNAME")
            mongo_pass = env.get("MONGO_INITDB_ROOT_PASSWORD")
            if mongo_user and mongo_pass:
                exec_cmd.extend([
                    "--username", self._sanitize_identifier(mongo_user),
                    "--password", mongo_pass,
                    "--authenticationDatabase", "admin",
                ])

            stream = self._exec_stream_to_gzip(container, exec_cmd, dump_path)
            return self._streamed_dump_result(db, "mongodb", "mongodump", dump_path, stream, None)
        except Exception as e:
            return DumpResult(
                container_name=db.container_name, db_type="mongodb",
                db_name=db.db_name, dump_path=dump_path, dump_size_bytes=0,
                integrity_check="skipped", status="failed", error=redact_credentials(str(e)),
            )

    def _dump_redis_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump Redis via BGSAVE (non-blocking) + LASTSAVE polling."""
        ts = self._timestamp()
        safe_container = re.sub(r'[^a-zA-Z0-9_.\-]', '_', db.container_name)
        dump_path = str(self.dump_dir / f"{safe_container}_redis_{ts}.rdb")

        try:
            container = self.docker.containers.get(db.container_name)

            # Get current LASTSAVE timestamp before triggering save
            exit_code, output = container.exec_run(["redis-cli", "LASTSAVE"])
            if exit_code != 0:
                error_msg = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
                return DumpResult(
                    container_name=db.container_name, db_type="redis",
                    db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                    integrity_check="skipped", status="failed",
                    error=f"redis-cli LASTSAVE failed: {error_msg[:200]}",
                )
            prev_lastsave = output.decode(errors="replace").strip().split()[-1] if isinstance(output, bytes) else str(output).strip().split()[-1]

            # Trigger BGSAVE (non-blocking)
            exit_code, output = container.exec_run(["redis-cli", "BGSAVE"])
            if exit_code != 0:
                error_msg = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
                return DumpResult(
                    container_name=db.container_name, db_type="redis",
                    db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                    integrity_check="skipped", status="failed",
                    error=f"redis-cli BGSAVE failed: {error_msg[:200]}",
                )

            # Poll LASTSAVE until it changes (max 60s)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                time.sleep(1)
                waited += 1
                exit_code, output = container.exec_run(["redis-cli", "LASTSAVE"])
                if exit_code != 0:
                    continue
                current_lastsave = output.decode(errors="replace").strip().split()[-1] if isinstance(output, bytes) else str(output).strip().split()[-1]
                if current_lastsave != prev_lastsave:
                    break
            else:
                return DumpResult(
                    container_name=db.container_name, db_type="redis",
                    db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                    integrity_check="skipped", status="failed",
                    error=f"BGSAVE did not complete within {max_wait}s",
                )

            # Validate that current_lastsave is a numeric timestamp (not an error string)
            if not current_lastsave.isdigit():
                logger.warning("redis_dump_invalid_lastsave container=%s lastsave=%s", db.container_name, current_lastsave)
                return DumpResult(
                    container_name=db.container_name, db_type="redis",
                    db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                    integrity_check="skipped", status="failed",
                    error=f"LASTSAVE returned non-numeric value: {current_lastsave[:200]}",
                )

            # Get the actual RDB file location via CONFIG GET
            exit_code, output = container.exec_run(["redis-cli", "CONFIG", "GET", "dir"])
            redis_dir = "/data"  # default
            if exit_code == 0:
                parts = output.decode(errors="replace").strip().split("\n") if isinstance(output, bytes) else str(output).strip().split("\n")
                if len(parts) >= 2:
                    redis_dir = parts[1].strip()

            exit_code, output = container.exec_run(["redis-cli", "CONFIG", "GET", "dbfilename"])
            rdb_filename = "dump.rdb"  # default
            if exit_code == 0:
                parts = output.decode(errors="replace").strip().split("\n") if isinstance(output, bytes) else str(output).strip().split("\n")
                if len(parts) >= 2:
                    rdb_filename = os.path.basename(parts[1].strip())  # Strip directory components
                    if not rdb_filename or rdb_filename.startswith('.'):
                        rdb_filename = "dump.rdb"  # Safe fallback

            # Find the host path for the Redis data directory via bind mounts
            rdb_container_path = os.path.join(redis_dir, rdb_filename)
            mounts = container.attrs.get("Mounts", [])

            for mount in mounts:
                source = mount.get("Source", "")
                destination = mount.get("Destination", "")
                # Check if the RDB file's directory matches this mount
                if rdb_container_path.startswith(destination):
                    relative = rdb_container_path[len(destination):].lstrip("/")
                    host_rdb = os.path.join(source, relative)
                    if os.path.exists(host_rdb):
                        try:
                            shutil.copy2(host_rdb, dump_path)
                        except Exception as e:
                            return DumpResult(
                                container_name=db.container_name, db_type="redis",
                                db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                                integrity_check="skipped", status="failed", error=str(e),
                            )
                        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                        return DumpResult(
                            container_name=db.container_name, db_type="redis",
                            db_name="redis", dump_path=dump_path, dump_size_bytes=size,
                            integrity_check="skipped", status="success" if size > 0 else "failed",
                        )

            # Fallback: scan all mount sources for the rdb filename
            for mount in mounts:
                source = mount.get("Source", "")
                rdb_path = os.path.join(source, rdb_filename)
                if os.path.exists(rdb_path):
                    try:
                        shutil.copy2(rdb_path, dump_path)
                    except Exception as e:
                        return DumpResult(
                            container_name=db.container_name, db_type="redis",
                            db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                            integrity_check="skipped", status="failed", error=str(e),
                        )
                    size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                    return DumpResult(
                        container_name=db.container_name, db_type="redis",
                        db_name="redis", dump_path=dump_path, dump_size_bytes=size,
                        integrity_check="skipped", status="success" if size > 0 else "failed",
                    )

            copied, archive_error = self._copy_file_from_container_archive(
                container, rdb_container_path, dump_path
            )
            if copied:
                size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                return DumpResult(
                    container_name=db.container_name, db_type="redis",
                    db_name="redis", dump_path=dump_path, dump_size_bytes=size,
                    integrity_check="skipped", status="success" if size > 0 else "failed",
                )

            return DumpResult(
                container_name=db.container_name, db_type="redis",
                db_name="redis", dump_path="", dump_size_bytes=0,
                integrity_check="skipped", status="failed",
                error=f"{rdb_filename} not found in container bind mounts and archive fallback failed: {archive_error}",
            )
        except Exception as e:
            return DumpResult(
                container_name=db.container_name, db_type="redis",
                db_name="redis", dump_path=dump_path, dump_size_bytes=0,
                integrity_check="skipped", status="failed", error=str(e),
            )

    def cleanup_old_dumps(self, keep_last: int = 1, max_age_days: int | None = 7) -> int:
        """Prune the dump directory. See ``cleanup_dump_dir``."""
        return cleanup_dump_dir(self.dump_dir, keep_last=keep_last, max_age_days=max_age_days)

    async def dump_single(self, container_name: str, db_name: str, db_type: str,
                          host_path: str | None = None, verify_integrity: bool = True) -> DumpResult:
        """Dump a single database on-demand."""
        db = DiscoveredDatabase(
            container_name=container_name,
            db_type=db_type,
            db_name=db_name,
            host_path=host_path,
        )
        return await self._dump_one(db)
