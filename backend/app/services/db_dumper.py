"""Database dump engine — safe dumps for Postgres, SQLite, MariaDB, MongoDB, Redis."""

import asyncio
import gzip
import io
import logging
import os
import re
import shlex
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import ArkiveConfig
from app.models.discovery import DiscoveredDatabase
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.db_dumper")


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

    def _timestamp(self) -> str:
        return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    async def dump_all(self, databases: list[DiscoveredDatabase]) -> list[DumpResult]:
        """Dump all discovered databases. Continue on failure."""
        results = []
        for db in databases:
            try:
                result = await self._dump_one(db)
                results.append(result)
            except Exception as e:
                logger.error("Failed to dump %s/%s: %s", db.container_name, db.db_name, e)
                results.append(
                    DumpResult(
                        container_name=db.container_name,
                        db_type=db.db_type,
                        db_name=db.db_name,
                        dump_path="",
                        dump_size_bytes=0,
                        integrity_check="skipped",
                        status="failed",
                        error=str(e),
                    )
                )
        return results

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
                container_name=db.container_name,
                db_type=db.db_type,
                db_name=db.db_name,
                dump_path="",
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
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
        if not value or not re.match(r"^[a-zA-Z0-9_.\-]+$", value):
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
        ts = self._timestamp()
        safe_container = re.sub(r"[^a-zA-Z0-9_.\-]", "_", db.container_name)
        safe_dbname = re.sub(r"[^a-zA-Z0-9_.\-]", "_", db.db_name)
        dump_path = str(self.dump_dir / f"{safe_container}_{safe_dbname}_{ts}.sql.gz")

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)
            pg_user = env.get("POSTGRES_USER") or env.get("DB_USERNAME") or env.get("DB_USER") or "postgres"
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
            exit_code, output = container.exec_run(exec_cmd, demux=True, stream=True)

            bytes_written = 0
            stderr_chunks: list[bytes] = []
            with gzip.open(dump_path, "wb") as f:
                for chunk in output:
                    if isinstance(chunk, tuple):
                        stdout_data, stderr_data = chunk
                        if stdout_data:
                            f.write(stdout_data)
                            bytes_written += len(stdout_data)
                        if stderr_data:
                            stderr_chunks.append(stderr_data)
                    elif chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)

            stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()

            if exit_code is not None and exit_code != 0:
                error_msg = stderr_text if stderr_text else f"pg_dump exited with code {exit_code}"
                logger.error(
                    "pg_dump for %s/%s failed (exit %s): %s",
                    db.container_name,
                    db.db_name,
                    exit_code,
                    stderr_text[:500],
                )
                return DumpResult(
                    container_name=db.container_name,
                    db_type="postgres",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"pg_dump exited with code {exit_code}: {error_msg}",
                )

            if bytes_written == 0:
                error_msg = stderr_text if stderr_text else "Empty dump file"
                return DumpResult(
                    container_name=db.container_name,
                    db_type="postgres",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=error_msg,
                )

            if bytes_written < 100:
                logger.warning(
                    "pg_dump for %s/%s produced suspiciously small output (%d bytes)",
                    db.container_name,
                    db.db_name,
                    bytes_written,
                )

            # Log stderr warnings even on success (pg_dump may emit warnings)
            if stderr_text:
                logger.warning("pg_dump stderr for %s/%s: %s", db.container_name, db.db_name, stderr_text[:500])

            size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
            return DumpResult(
                container_name=db.container_name,
                db_type="postgres",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=size,
                integrity_check="skipped",
                status="success",
            )
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="postgres",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=str(e),
            )

    async def _dump_sqlite(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump SQLite using HOST sqlite3 binary on bind-mounted path."""
        ts = self._timestamp()
        # Sanitize container_name and db_name for safe filename construction
        safe_container = re.sub(r"[^a-zA-Z0-9_.\-]", "_", db.container_name)
        safe_dbname = re.sub(r"[^a-zA-Z0-9_.\-]", "_", db.db_name)
        dump_path = str(self.dump_dir / f"{safe_container}_{safe_dbname}_{ts}.sqlite3")

        if not db.host_path:
            return DumpResult(
                container_name=db.container_name,
                db_type="sqlite",
                db_name=db.db_name,
                dump_path="",
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error="No host path available for SQLite backup",
            )

        # Validate host_path is absolute and does not contain traversal
        host_path = os.path.normpath(db.host_path)
        if not os.path.isabs(host_path) or ".." in db.host_path:
            return DumpResult(
                container_name=db.container_name,
                db_type="sqlite",
                db_name=db.db_name,
                dump_path="",
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
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
                    container_name=db.container_name,
                    db_type="sqlite",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="failed",
                    status="failed",
                    error=combined,
                )

        if result.returncode != 0:
            return DumpResult(
                container_name=db.container_name,
                db_type="sqlite",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="failed",
                status="failed",
                error=result.stderr,
            )

        # Integrity check
        integrity = "skipped"
        check_result = await run_command(["sqlite3", dump_path, "PRAGMA integrity_check"])
        if check_result.returncode == 0 and "ok" in check_result.stdout.lower():
            integrity = "ok"
        elif check_result.returncode != 0:
            integrity = "failed"

        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
        return DumpResult(
            container_name=db.container_name,
            db_type="sqlite",
            db_name=db.db_name,
            dump_path=dump_path,
            dump_size_bytes=size,
            integrity_check=integrity,
            status="success" if size > 0 else "failed",
        )

    def _dump_mariadb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MariaDB/MySQL via docker exec with streaming."""
        ts = self._timestamp()
        dump_path = str(self.dump_dir / f"{db.container_name}_{db.db_name}_{ts}.sql.gz")

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
            exec_cmd = [dump_binary, "-u", mysql_user]
            if mysql_password:
                exec_env["MYSQL_PWD"] = mysql_password
            exec_cmd.extend(["--databases", db_name])
            exit_code, output = container.exec_run(exec_cmd, demux=True, stream=True, environment=exec_env)

            bytes_written = 0
            stderr_chunks: list[bytes] = []
            with gzip.open(dump_path, "wb") as f:
                for chunk in output:
                    if isinstance(chunk, tuple):
                        stdout_data, stderr_data = chunk
                        if stdout_data:
                            f.write(stdout_data)
                            bytes_written += len(stdout_data)
                        if stderr_data:
                            stderr_chunks.append(stderr_data)
                    elif chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)

            stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()

            if exit_code is not None and exit_code != 0:
                error_msg = stderr_text if stderr_text else f"mysqldump exited with code {exit_code}"
                logger.error(
                    "mysqldump for %s/%s failed (exit %s): %s",
                    db.container_name,
                    db.db_name,
                    exit_code,
                    stderr_text[:500],
                )
                return DumpResult(
                    container_name=db.container_name,
                    db_type="mariadb",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"mysqldump exited with code {exit_code}: {error_msg}",
                )

            if bytes_written == 0:
                error_msg = stderr_text if stderr_text else "Empty dump file"
                return DumpResult(
                    container_name=db.container_name,
                    db_type="mariadb",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=error_msg,
                )

            if bytes_written < 100:
                logger.warning(
                    "mysqldump for %s/%s produced suspiciously small output (%d bytes)",
                    db.container_name,
                    db.db_name,
                    bytes_written,
                )

            # Log stderr warnings even on success (mysqldump may emit warnings)
            if stderr_text:
                logger.warning("mysqldump stderr for %s/%s: %s", db.container_name, db.db_name, stderr_text[:500])

            size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
            return DumpResult(
                container_name=db.container_name,
                db_type="mariadb",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=size,
                integrity_check="skipped",
                status="success",
            )
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="mariadb",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=str(e),
            )

    def _dump_mongodb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MongoDB via docker exec with authentication support."""
        ts = self._timestamp()
        dump_path = str(self.dump_dir / f"{db.container_name}_{db.db_name}_{ts}.archive.gz")

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)

            # Build mongodump command with authentication if credentials are available
            exec_cmd = ["mongodump", "--archive"]
            mongo_user = env.get("MONGO_INITDB_ROOT_USERNAME")
            mongo_pass = env.get("MONGO_INITDB_ROOT_PASSWORD")
            if mongo_user and mongo_pass:
                exec_cmd.extend(
                    [
                        "--username",
                        self._sanitize_identifier(mongo_user),
                        "--password",
                        mongo_pass,
                        "--authenticationDatabase",
                        "admin",
                    ]
                )

            exit_code, output = container.exec_run(exec_cmd, demux=True, stream=True)

            bytes_written = 0
            stderr_chunks: list[bytes] = []
            with gzip.open(dump_path, "wb") as f:
                for chunk in output:
                    if isinstance(chunk, tuple):
                        stdout_data, stderr_data = chunk
                        if stdout_data:
                            f.write(stdout_data)
                            bytes_written += len(stdout_data)
                        if stderr_data:
                            stderr_chunks.append(stderr_data)
                    elif chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)

            stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()

            if exit_code is not None and exit_code != 0:
                error_msg = stderr_text if stderr_text else f"mongodump exited with code {exit_code}"
                logger.error(
                    "mongodump for %s/%s failed (exit %s): %s",
                    db.container_name,
                    db.db_name,
                    exit_code,
                    stderr_text[:500],
                )
                return DumpResult(
                    container_name=db.container_name,
                    db_type="mongodb",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"mongodump exited with code {exit_code}: {error_msg}",
                )

            if bytes_written == 0:
                error_msg = stderr_text if stderr_text else "Empty dump file"
                return DumpResult(
                    container_name=db.container_name,
                    db_type="mongodb",
                    db_name=db.db_name,
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=error_msg,
                )

            if bytes_written < 100:
                logger.warning(
                    "mongodump for %s/%s produced suspiciously small output (%d bytes)",
                    db.container_name,
                    db.db_name,
                    bytes_written,
                )

            # Log stderr warnings even on success (mongodump writes progress to stderr)
            if stderr_text and "error" in stderr_text.lower():
                logger.warning("mongodump stderr for %s/%s: %s", db.container_name, db.db_name, stderr_text[:500])

            size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
            return DumpResult(
                container_name=db.container_name,
                db_type="mongodb",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=size,
                integrity_check="skipped",
                status="success",
            )
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="mongodb",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=str(e),
            )

    def _dump_redis_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump Redis via BGSAVE (non-blocking) + LASTSAVE polling."""
        ts = self._timestamp()
        safe_container = re.sub(r"[^a-zA-Z0-9_.\-]", "_", db.container_name)
        dump_path = str(self.dump_dir / f"{safe_container}_redis_{ts}.rdb")

        try:
            container = self.docker.containers.get(db.container_name)

            # Get current LASTSAVE timestamp before triggering save
            exit_code, output = container.exec_run(["redis-cli", "LASTSAVE"])
            if exit_code != 0:
                error_msg = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
                return DumpResult(
                    container_name=db.container_name,
                    db_type="redis",
                    db_name="redis",
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"redis-cli LASTSAVE failed: {error_msg[:200]}",
                )
            prev_lastsave = (
                output.decode(errors="replace").strip().split()[-1]
                if isinstance(output, bytes)
                else str(output).strip().split()[-1]
            )

            # Trigger BGSAVE (non-blocking)
            exit_code, output = container.exec_run(["redis-cli", "BGSAVE"])
            if exit_code != 0:
                error_msg = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
                return DumpResult(
                    container_name=db.container_name,
                    db_type="redis",
                    db_name="redis",
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
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
                current_lastsave = (
                    output.decode(errors="replace").strip().split()[-1]
                    if isinstance(output, bytes)
                    else str(output).strip().split()[-1]
                )
                if current_lastsave != prev_lastsave:
                    break
            else:
                return DumpResult(
                    container_name=db.container_name,
                    db_type="redis",
                    db_name="redis",
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"BGSAVE did not complete within {max_wait}s",
                )

            # Validate that current_lastsave is a numeric timestamp (not an error string)
            if not current_lastsave.isdigit():
                logger.warning(
                    "redis_dump_invalid_lastsave container=%s lastsave=%s", db.container_name, current_lastsave
                )
                return DumpResult(
                    container_name=db.container_name,
                    db_type="redis",
                    db_name="redis",
                    dump_path=dump_path,
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=f"LASTSAVE returned non-numeric value: {current_lastsave[:200]}",
                )

            # Get the actual RDB file location via CONFIG GET
            exit_code, output = container.exec_run(["redis-cli", "CONFIG", "GET", "dir"])
            redis_dir = "/data"  # default
            if exit_code == 0:
                parts = (
                    output.decode(errors="replace").strip().split("\n")
                    if isinstance(output, bytes)
                    else str(output).strip().split("\n")
                )
                if len(parts) >= 2:
                    redis_dir = parts[1].strip()

            exit_code, output = container.exec_run(["redis-cli", "CONFIG", "GET", "dbfilename"])
            rdb_filename = "dump.rdb"  # default
            if exit_code == 0:
                parts = (
                    output.decode(errors="replace").strip().split("\n")
                    if isinstance(output, bytes)
                    else str(output).strip().split("\n")
                )
                if len(parts) >= 2:
                    rdb_filename = os.path.basename(parts[1].strip())  # Strip directory components
                    if not rdb_filename or rdb_filename.startswith("."):
                        rdb_filename = "dump.rdb"  # Safe fallback

            # Find the host path for the Redis data directory via bind mounts
            rdb_container_path = os.path.join(redis_dir, rdb_filename)
            mounts = container.attrs.get("Mounts", [])

            for mount in mounts:
                source = mount.get("Source", "")
                destination = mount.get("Destination", "")
                # Check if the RDB file's directory matches this mount
                if rdb_container_path.startswith(destination):
                    relative = rdb_container_path[len(destination) :].lstrip("/")
                    host_rdb = os.path.join(source, relative)
                    if os.path.exists(host_rdb):
                        try:
                            shutil.copy2(host_rdb, dump_path)
                        except Exception as e:
                            return DumpResult(
                                container_name=db.container_name,
                                db_type="redis",
                                db_name="redis",
                                dump_path=dump_path,
                                dump_size_bytes=0,
                                integrity_check="skipped",
                                status="failed",
                                error=str(e),
                            )
                        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                        return DumpResult(
                            container_name=db.container_name,
                            db_type="redis",
                            db_name="redis",
                            dump_path=dump_path,
                            dump_size_bytes=size,
                            integrity_check="skipped",
                            status="success" if size > 0 else "failed",
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
                            container_name=db.container_name,
                            db_type="redis",
                            db_name="redis",
                            dump_path=dump_path,
                            dump_size_bytes=0,
                            integrity_check="skipped",
                            status="failed",
                            error=str(e),
                        )
                    size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                    return DumpResult(
                        container_name=db.container_name,
                        db_type="redis",
                        db_name="redis",
                        dump_path=dump_path,
                        dump_size_bytes=size,
                        integrity_check="skipped",
                        status="success" if size > 0 else "failed",
                    )

            copied, archive_error = self._copy_file_from_container_archive(container, rdb_container_path, dump_path)
            if copied:
                size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
                return DumpResult(
                    container_name=db.container_name,
                    db_type="redis",
                    db_name="redis",
                    dump_path=dump_path,
                    dump_size_bytes=size,
                    integrity_check="skipped",
                    status="success" if size > 0 else "failed",
                )

            return DumpResult(
                container_name=db.container_name,
                db_type="redis",
                db_name="redis",
                dump_path="",
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=f"{rdb_filename} not found in container bind mounts and archive fallback failed: {archive_error}",
            )
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="redis",
                db_name="redis",
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=str(e),
            )

    def cleanup_old_dumps(self, keep_last: int = 3) -> int:
        """Remove old dump files, keeping only the last N per container/db combo.

        Returns the number of files removed. This prevents unbounded growth
        of the dump directory across successive backup runs.
        """
        import glob
        import re
        from collections import defaultdict

        if keep_last < 1:
            keep_last = 1

        removed = 0
        # Group dump files by their container_db prefix (everything before the timestamp)
        # Filenames: {container}_{dbname}_{YYYYMMDD}_{HHMMSS}.ext
        ts_pattern = re.compile(r"^(.+?)_(\d{8}_\d{6})\.")
        dump_files: dict[str, list[str]] = defaultdict(list)

        all_files = sorted(glob.glob(str(self.dump_dir / "*")), reverse=True)
        for fpath in all_files:
            if not os.path.isfile(fpath):
                continue
            basename = os.path.basename(fpath)
            match = ts_pattern.match(basename)
            if match:
                prefix = match.group(1)
            else:
                prefix = basename
            dump_files[prefix].append(fpath)

        for prefix, files in dump_files.items():
            # Files are already sorted newest-first (reverse glob on path)
            for old_file in files[keep_last:]:
                try:
                    os.remove(old_file)
                    removed += 1
                    logger.info("Removed old dump: %s", old_file)
                except OSError as e:
                    logger.warning("Failed to remove old dump %s: %s", old_file, e)

        if removed:
            logger.info("Dump cleanup: removed %d old dump files", removed)
        return removed

    async def dump_single(
        self,
        container_name: str,
        db_name: str,
        db_type: str,
        host_path: str | None = None,
        verify_integrity: bool = True,
    ) -> DumpResult:
        """Dump a single database on-demand."""
        db = DiscoveredDatabase(
            container_name=container_name,
            db_type=db_type,
            db_name=db_name,
            host_path=host_path,
        )
        return await self._dump_one(db)
