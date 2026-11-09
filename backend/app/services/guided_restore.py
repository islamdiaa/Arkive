"""Guided in-place restore of a single database.

A file restore (``POST /api/restore``) only writes into ``/config/restores``;
loading a dump back into the live database was left to the user. A guided
restore does the whole job for one discovered database:

1. checks the source container (running, client tools, privileges);
2. finds the database's dump in the chosen snapshot (dump manifest first,
   dump file names for older snapshots) and restores only that file into
   ``/config/restores/guided-<id>/``;
3. checks the dump (completion trailer, the database it creates);
4. takes a fresh *safety dump* of the current database into the same folder,
   so the restore can be undone (``undo`` loads it back the same way);
5. optionally stops the apps that use the database;
6. streams the dump into the container's own client over ``docker exec``
   stdin (psql / mariadb / mysql): never copied into the container, never run
   through a shell, and no user input in an argv position that could be read
   as an option. Postgres sessions on the database are ended first and the
   original database owner is put back afterwards;
7. counts the restored tables and starts the stopped apps again.

The operation holds the backup lock from start to finish (no backup, dump,
drill or other restore runs meanwhile) and is recorded in ``guided_restores``
with a per-step log.

SQLite databases are files under /mnt/user, which Arkive can (and should) have
mounted read-only, so Arkive never writes them: it restores and integrity-checks
the file, keeps a safety copy of the current one and returns the exact steps to
swap it in (status ``action_required``).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from docker.utils.socket import STDERR, frames_iter

from app.core.activity import log_activity
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.models.discovery import DiscoveredDatabase
from app.services.backup_engine import ResticError
from app.services.db_dumper import DBDumper, dump_prefix, legacy_dump_prefix, postgres_user_from_env
from app.services.lock_manager import BackupLockManager
from app.services.restore_drill import (
    DUMP_MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    DrillFailure,
    DrillSkip,
    prepare_sql_dump,
    valid_pg_dbname,
    verify_sqlite_file,
)
from app.utils.mounts import host_path_for, running_in_container
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.guided_restore")

MIB = 1024 * 1024

WORK_DIR_PREFIX = "guided-"
# Ownership marker in every work dir; retention only deletes directories that
# carry it with their own id (users may restore files into restore_dir too).
WORK_MARKER = ".arkive-guided-restore"
_WORK_DIR_RE = re.compile(r"^guided-([0-9a-f]{32})$")
# Scratch space for previews (reading a snapshot's dump manifest); removed
# right away, and by retention if a crash left one behind.
PREVIEW_DIR_PREFIX = ".guided-preview-"
PREVIEW_MARKER = "preview"

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_WARNINGS = "completed_with_warnings"
STATUS_FAILED = "failed"
STATUS_ACTION_REQUIRED = "action_required"

METHOD_LOAD = "load"
METHOD_MANUAL = "manual"
METHOD_UNSUPPORTED = "unsupported"

LOAD_TYPES = frozenset({"postgres", "mariadb"})
MANUAL_TYPES = frozenset({"sqlite"})
MYSQL_SYSTEM_SCHEMAS = frozenset({"mysql", "information_schema", "performance_schema", "sys"})

# What the dumper can dump (DBDumper._sanitize_identifier), minus a leading "-".
_MYSQL_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.\-]*$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.\-]*$")
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_DUMP_FILE_RE = re.compile(r"^(?P<prefix>.+)_(?P<ts>\d{8}_\d{6})\.(?P<ext>[A-Za-z0-9.]+)$")
_MYSQL_USE_RE = re.compile(rb"^USE `((?:[^`]|``)+)`;")
_DUMP_EXTENSIONS = {"postgres": ("sql", "sql.gz"), "mariadb": ("sql", "sql.gz"), "sqlite": ("sqlite3",)}

_STEP_LABELS = {
    "check_container": "Check the database container",
    "locate_dump": "Find the dump in the snapshot",
    "restore_dump": "Restore the dump from the repository",
    "verify_dump": "Check the dump is complete",
    "safety_dump": "Take a safety dump of the current database",
    "stop_apps": "Stop the apps that use the database",
    "load": "Load the dump into the database",
    "verify": "Check the restored database",
    "start_apps": "Start the apps again",
    "instructions": "Swap the file in by hand",
}
_LOAD_STEPS = (
    "check_container",
    "locate_dump",
    "restore_dump",
    "verify_dump",
    "safety_dump",
    "stop_apps",
    "load",
    "verify",
    "start_apps",
)
_UNDO_STEPS = ("check_container", "verify_dump", "safety_dump", "stop_apps", "load", "verify", "start_apps")
_MANUAL_STEPS = ("locate_dump", "restore_dump", "verify_dump", "safety_dump", "instructions")

# Restored-table count in a Postgres database (user tables, no extension members).
_PG_COUNT_SQL = (
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
    "AND d.objid = c.oid AND d.deptype = 'e')"
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mb(size_bytes: int) -> int:
    return max(0, round(size_bytes / MIB))


def _size(size_bytes: int) -> str:
    if size_bytes >= 1024 * MIB:
        return f"{size_bytes / (1024 * MIB):.1f} GB"
    if size_bytes >= MIB:
        return f"{size_bytes / MIB:.1f} MB"
    return f"{max(1, round(size_bytes / 1024))} KB"


def _clip(text: str, limit: int = 600) -> str:
    """Tail of a database tool's output, with anything that looks like a credential masked.

    Only tool output goes through redaction: its "-p<value>" rule would also
    mangle container names such as "immich-postgres" in Arkive's own messages.
    """
    return redact_credentials(text.strip())[-limit:]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


class GuidedRestoreError(Exception):
    """A request that cannot be carried out; ``status_code`` is the HTTP status."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class _StepFailed(Exception):  # noqa: N818 - control flow, not an error
    """Stop the operation at the current step with this message."""


class _StepSkipped(Exception):  # noqa: N818 - control flow, not an error
    """The step does not apply; recorded as skipped with this detail."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_db_type(value: Any) -> str:
    db_type = str(value or "").strip().lower()
    return "mariadb" if db_type == "mysql" else db_type


def restore_method(db_type: str) -> tuple[str, str | None]:
    """How a database of ``db_type`` is restored: (method, reason if unsupported)."""
    db_type = normalize_db_type(db_type)
    if db_type in LOAD_TYPES:
        return METHOD_LOAD, None
    if db_type in MANUAL_TYPES:
        return METHOD_MANUAL, None
    tool = {"mongodb": "mongorestore", "redis": "the Redis RDB file"}.get(db_type)
    hint = f" with {tool}" if tool else ""
    return METHOD_UNSUPPORTED, (
        f"Guided restore does not support {db_type or 'this database type'} yet. Restore the dump "
        f"from the snapshot browser (it lands in /config/restores) and load it{hint} by hand."
    )


def pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def pg_literal(value: str) -> str:
    # standard_conforming_strings is on by default (PostgreSQL 9.1+).
    return "'" + value.replace("'", "''") + "'"


def mysql_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def mysql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


@dataclass
class DatabaseRef:
    container_name: str
    db_type: str
    db_name: str
    host_path: str | None = None

    @property
    def label(self) -> str:
        return f"{self.container_name}/{self.db_name}"

    def discovered(self) -> DiscoveredDatabase:
        return DiscoveredDatabase(
            container_name=self.container_name, db_type=self.db_type, db_name=self.db_name, host_path=self.host_path
        )


def validate_ref(ref: DatabaseRef) -> None:
    """Refuse names that could be misread by a client tool or that are unsafe to restore."""
    if not _CONTAINER_NAME_RE.match(ref.container_name):
        raise GuidedRestoreError(f"Invalid container name: {ref.container_name!r}")
    if ref.db_type == "postgres":
        if not valid_pg_dbname(ref.db_name) or len(ref.db_name.encode()) > 63:
            raise GuidedRestoreError(f"Database name {ref.db_name!r} cannot be restored safely")
    elif ref.db_type == "mariadb":
        if not _MYSQL_NAME_RE.match(ref.db_name):
            raise GuidedRestoreError(f"Database name {ref.db_name!r} cannot be restored safely")
        if ref.db_name.lower() in MYSQL_SYSTEM_SCHEMAS:
            raise GuidedRestoreError(
                f"{ref.db_name} is a MySQL/MariaDB system schema; restoring it in place is not supported"
            )
    elif ref.db_type == "sqlite":
        path = ref.host_path or ""
        if not path.startswith("/") or ".." in Path(path).parts or os.path.normpath(path) != path.rstrip("/"):
            raise GuidedRestoreError("The SQLite database has no valid absolute path")


def postgres_prelude(db_name: str, exists: bool) -> bytes:
    """psql script run before a Postgres dump: block new sessions, end current ones.

    The dump itself (pg_dump --clean --create) then drops and re-creates the
    database; DROP DATABASE fails while anyone else is connected.
    """
    lines = ["\\set ON_ERROR_STOP on"]
    if exists:
        lines += [
            f"ALTER DATABASE {pg_ident(db_name)} WITH ALLOW_CONNECTIONS false;",
            "\\o /dev/null",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # nosec B608 - names quoted by pg_literal
            f"WHERE datname = {pg_literal(db_name)} AND pid <> pg_backend_pid();",
            "\\o",
        ]
    return ("\n".join(lines) + "\n").encode()


def postgres_state_sql(db_name: str) -> str:
    lit = pg_literal(db_name)
    return (
        "SELECT json_build_object("  # nosec B608 - names quoted by pg_literal
        "'user', current_user, "
        "'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
        "'createdb', (SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user), "
        "'version', current_setting('server_version_num')::int, "
        f"'exists', EXISTS (SELECT 1 FROM pg_database WHERE datname = {lit}), "
        f"'oid', (SELECT oid::text FROM pg_database WHERE datname = {lit}), "
        f"'allowconn', (SELECT datallowconn FROM pg_database WHERE datname = {lit}), "
        f"'owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = {lit}), "
        f"'sessions', (SELECT count(*) FROM pg_stat_activity WHERE datname = {lit} AND pid <> pg_backend_pid()))"
    )


def postgres_owner_sql(db_name: str, owner: str, server_version_num: int) -> str:
    """Give the restored database and its objects back to ``owner``.

    Dumps are taken with --no-owner, so a load creates everything as the role
    Arkive connects as. When the database belonged to another role (an app
    role next to a ``postgres`` superuser), the app would lose access to its
    own tables. Extension members and column-owned sequences are skipped
    (they follow their extension / table).
    """
    owner_lit = pg_literal(owner)
    me = "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
    user_ns = "n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'"

    def not_extension_member(catalog: str, alias: str) -> str:
        return (
            f"NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{catalog}'::regclass "  # nosec B608 - constant catalog names
            f"AND d.objid = {alias}.oid AND d.deptype = 'e')"
        )

    if server_version_num >= 110000:
        proc_kind = "CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END"
    else:
        proc_kind = "CASE WHEN p.proisagg THEN 'AGGREGATE' ELSE 'FUNCTION' END"
    statements = [
        # Schemas (public stays as it was created).
        f"SELECT format('ALTER SCHEMA %I OWNER TO %I', n.nspname, {owner_lit}) FROM pg_namespace n "  # nosec B608 - owner quoted by pg_literal
        f"WHERE n.nspowner = {me} AND n.nspname <> 'public' AND {user_ns} "
        f"AND {not_extension_member('pg_namespace', 'n')}",
        # Tables, views, materialized views, standalone sequences, foreign tables.
        "SELECT format('ALTER %s %s OWNER TO %I', CASE c.relkind WHEN 'v' THEN 'VIEW' "  # nosec B608 - owner quoted by pg_literal
        "WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' "
        f"ELSE 'TABLE' END, c.oid::regclass, {owner_lit}) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE c.relowner = {me} AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND {user_ns} "
        f"AND {not_extension_member('pg_class', 'c')} "
        "AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
        "AND d.objid = c.oid AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')))",
        # Functions, procedures, aggregates.
        f"SELECT format('ALTER %s %s OWNER TO %I', {proc_kind}, p.oid::regprocedure, {owner_lit}) "  # nosec B608 - owner quoted by pg_literal
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE p.proowner = {me} AND {user_ns} AND {not_extension_member('pg_proc', 'p')}",
        # Enums, domains, ranges and standalone composite types (not array or table row types).
        "SELECT format('ALTER %s %s OWNER TO %I', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, "  # nosec B608 - owner quoted by pg_literal
        f"t.oid::regtype, {owner_lit}) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        f"WHERE t.typowner = {me} AND t.typtype IN ('e', 'd', 'r', 'c') AND {user_ns} "
        "AND (t.typrelid = 0 OR (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c') "
        "AND NOT EXISTS (SELECT 1 FROM pg_type el WHERE el.typarray = t.oid) "
        f"AND {not_extension_member('pg_type', 't')}",
        # Large objects.
        f"SELECT format('ALTER LARGE OBJECT %s OWNER TO %I', l.oid, {owner_lit}) "  # nosec B608 - owner quoted by pg_literal
        f"FROM pg_largeobject_metadata l WHERE l.lomowner = {me}",
    ]
    union = " UNION ALL ".join(statements)
    return (
        f"ALTER DATABASE {pg_ident(db_name)} OWNER TO {pg_ident(owner)}; "  # nosec B608 - names quoted by pg_ident
        "DO $arkive$ DECLARE r record; BEGIN "
        f"FOR r IN SELECT stmt FROM ({union}) AS s(stmt) LOOP EXECUTE r.stmt; END LOOP; "
        "END $arkive$;"
    )


def mysql_prelude(db_name: str, exists: bool) -> bytes:
    """SQL run before a MySQL/MariaDB dump: drop the database's tables and views.

    mysqldump re-creates every table it contains but would leave tables that
    exist now and not in the dump; the database itself (and its stored
    routines, which dumps don't include) is kept.
    """
    if not exists:
        return b""
    schema = mysql_literal(db_name)

    def drop(kind: str, types: str) -> str:
        return (
            "SET @arkive_names = (SELECT GROUP_CONCAT(CONCAT('`', REPLACE(TABLE_NAME, '`', '``'), '`') "  # nosec B608 - schema quoted by mysql_literal
            "SEPARATOR ', ') FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = {schema} AND TABLE_TYPE IN ({types}));\n"
            f"SET @arkive_sql = IF(@arkive_names IS NULL, 'DO 0', CONCAT('DROP {kind} IF EXISTS ', @arkive_names));\n"
            "PREPARE arkive_stmt FROM @arkive_sql;\nEXECUTE arkive_stmt;\nDEALLOCATE PREPARE arkive_stmt;\n"
        )

    return (
        "SET FOREIGN_KEY_CHECKS = 0;\n"
        "SET SESSION group_concat_max_len = 1073741824;\n"
        f"USE {mysql_ident(db_name)};\n"
        + drop("VIEW", "'VIEW'")
        + drop("TABLE", "'BASE TABLE', 'SYSTEM VERSIONED'")
        + drop("SEQUENCE", "'SEQUENCE'")
    ).encode()


def mysql_dump_databases(path: Path) -> set[str]:
    """Databases a mysqldump file switches to (``USE `name`;`` lines)."""
    found: set[str] = set()
    with open(path, "rb") as fh:
        at_line_start = True
        while True:
            line = fh.readline(MIB)
            if not line:
                break
            starts_line, at_line_start = at_line_start, line.endswith(b"\n")
            if starts_line and line.startswith(b"USE `"):
                match = _MYSQL_USE_RE.match(line)
                if match:
                    found.add(match.group(1).replace(b"``", b"`").decode("utf-8", errors="replace"))
    return found


def mysql_credentials(env: dict[str, str]) -> tuple[str, str]:
    """User/password the way DBDumper picks them for mysqldump."""
    root_password = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD")
    app_user = env.get("MYSQL_USER") or env.get("MARIADB_USER")
    app_password = env.get("MYSQL_PASSWORD") or env.get("MARIADB_PASSWORD")
    if root_password:
        return "root", root_password
    if app_user and app_password:
        return app_user, app_password
    return "root", ""


def sqlite_instructions(
    ref: DatabaseRef, restored_path: str, safety_path: str | None, stamp: str
) -> list[dict[str, str | None]]:
    """Hand-run steps to swap a restored SQLite file in (paths shell-quoted)."""
    live = ref.host_path or ""
    q = shlex.quote
    backup_cmd = (
        f"for f in {q(live)} {q(live + '-wal')} {q(live + '-shm')}; do "
        f'[ -e "$f" ] && cp -a "$f" "$f.before-restore-{stamp}"; done'
    )
    steps: list[dict[str, str | None]] = [
        {
            "text": f"Stop the {ref.container_name} container (Docker tab, or run this on the server):",
            "command": f"docker stop {q(ref.container_name)}",
        },
        {
            "text": "Keep a copy of the current database files next to them:",
            "command": backup_cmd,
        },
        {
            "text": (
                "Remove the current write-ahead log files. They belong to the current database and "
                "would corrupt the restored one if left in place:"
            ),
            "command": f"rm -f {q(live + '-wal')} {q(live + '-shm')}",
        },
        {
            "text": "Copy the restored database over the current one (the file keeps its owner and permissions):",
            "command": f"cp {q(restored_path)} {q(live)}",
        },
        {
            "text": f"Start the {ref.container_name} container again:",
            "command": f"docker start {q(ref.container_name)}",
        },
    ]
    if safety_path:
        steps.append(
            {
                "text": (
                    "Arkive also saved a consistent copy of the database as it was before this restore. "
                    "To go back, repeat the steps with this file instead:"
                ),
                "command": safety_path,
            }
        )
    return steps


def _file_chunks(path: Path, size: int = MIB) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while chunk := fh.read(size):
            yield chunk


class _Tail:
    """Keeps the last ``limit`` bytes written to it."""

    def __init__(self, limit: int = 64 * 1024):
        self.limit = limit
        self.data = b""

    def add(self, chunk: bytes) -> None:
        self.data = (self.data + chunk)[-self.limit :]

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


@dataclass
class StdinExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    write_error: str | None = None


def exec_with_stdin(
    container,
    cmd: list[str],
    env: dict[str, str] | None,
    chunks: Iterable[bytes],
    progress: Callable[[int], None] | None = None,
    finish_timeout: float = 60.0,
    poll_seconds: float = 0.5,
) -> StdinExecResult:
    """Run ``cmd`` in ``container`` with ``chunks`` streamed to its stdin.

    The equivalent of ``docker exec -i container cmd < file`` through the
    low-level exec API: stdout/stderr are drained by a reader thread (only a
    tail is kept) while the input is written, the write side is shut down for
    EOF, and the real exit code comes from ``exec_inspect``. No shell.
    """
    api = container.client.api
    exec_id = api.exec_create(container.id, cmd, stdin=True, stdout=True, stderr=True, environment=env or None)["Id"]
    sock = api.exec_start(exec_id, socket=True)
    raw = getattr(sock, "_sock", sock)
    try:
        # The client's read timeout must not apply while the database is busy
        # (e.g. building indexes) and not reading its input.
        raw.settimeout(None)
    except OSError:
        pass
    out, err = _Tail(), _Tail()
    reader_errors: list[BaseException] = []

    def _read() -> None:
        try:
            for stream_id, data in frames_iter(sock, tty=False):
                (err if stream_id == STDERR else out).add(data)
        except BaseException as exc:  # noqa: BLE001 - reported below
            reader_errors.append(exc)

    reader = threading.Thread(target=_read, name="guided-restore-exec-reader", daemon=True)
    reader.start()
    write_error = None
    try:
        for chunk in chunks:
            if not chunk:
                continue
            raw.sendall(chunk)
            if progress is not None:
                progress(len(chunk))
    except OSError as exc:
        # The process stopped reading (exited early, usually on an error); its
        # output and exit code say why.
        write_error = str(exc) or type(exc).__name__
    finally:
        try:
            raw.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    reader.join()
    for closable in (sock, raw):
        try:
            closable.close()
        except Exception:
            pass

    exit_code = None
    deadline = time.monotonic() + finish_timeout
    while True:
        info = api.exec_inspect(exec_id) or {}
        if not info.get("Running"):
            exit_code = info.get("ExitCode")
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)
    stderr = err.text()
    if reader_errors and not stderr:
        stderr = f"reading the command output failed: {reader_errors[0]}"
    return StdinExecResult(exit_code=exit_code, stdout=out.text(), stderr=stderr, write_error=write_error)


def _exec(container, cmd: list[str], env: dict[str, str] | None = None) -> tuple[int | None, str, str]:
    """Short command: (exit code, stdout, stderr). Non-streaming exec_run reports the real exit code."""
    result = container.exec_run(cmd, environment=env or None, demux=True)
    code = result.exit_code if hasattr(result, "exit_code") else result[0]
    output = result.output if hasattr(result, "output") else result[1]
    stdout, stderr = output if isinstance(output, tuple) else (output, b"")

    def text(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    return code, text(stdout), text(stderr)


def _container_env(container) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in (getattr(container, "attrs", None) or {}).get("Config", {}).get("Env") or []:
        key, sep, value = str(item).partition("=")
        if sep:
            env[key] = value
    return env


def _is_docker_unavailable(exc: BaseException) -> bool:
    return type(exc).__name__ == "DockerUnavailableError"


# ---------------------------------------------------------------------------
# Database drivers (blocking; run in worker threads)
# ---------------------------------------------------------------------------


class _PostgresDriver:
    db_type = "postgres"
    tool = "psql"

    def __init__(self, ref: DatabaseRef, owner_hint: str | None = None):
        self.ref = ref
        self.user = "postgres"
        self.env: dict[str, str] = {}
        self.maintenance_db = "postgres"
        self.state: dict[str, Any] = {}
        # Undo: the owner the database had before the restore being undone
        # (a failed load leaves it re-created and owned by the connecting role).
        self.owner_hint = owner_hint

    @property
    def original_owner(self) -> str | None:
        return self.owner_hint or self.state.get("owner")

    @property
    def exists(self) -> bool:
        return bool(self.state.get("exists"))

    def _psql(self, container, database: str, sql: str) -> tuple[int | None, str, str]:
        return _exec(
            container,
            ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", database, "-c", sql],
            self.env,
        )

    def _query_state(self, container) -> dict[str, Any]:
        code, out, err = self._psql(container, self.maintenance_db, postgres_state_sql(self.ref.db_name))
        if code != 0:
            raise _StepFailed(f"psql in {self.ref.container_name} failed: {_clip(err or out) or f'exit {code}'}")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError) as exc:
            raise _StepFailed(f"unexpected psql output: {_clip(out, 200)}") from exc

    def preflight(self, container) -> str:
        env = _container_env(container)
        user = postgres_user_from_env(env)
        if not _ROLE_RE.match(user):
            raise _StepFailed(f"the Postgres user {user!r} from the container environment is not supported")
        self.user = user
        self.env = {"PGCONNECT_TIMEOUT": "10"}
        if env.get("POSTGRES_PASSWORD"):
            self.env["PGPASSWORD"] = env["POSTGRES_PASSWORD"]
        candidates = ["template1"] if self.ref.db_name == "postgres" else ["postgres", "template1"]
        last_error = ""
        for maintenance_db in candidates:
            self.maintenance_db = maintenance_db
            code, out, err = self._psql(container, maintenance_db, "SELECT 1")
            if code == 0:
                break
            last_error = _clip(err or out) or f"exit code {code}"
            if "executable file not found" in last_error or code in (126, 127):
                raise _StepFailed(f"psql is not available in {self.ref.container_name}: {last_error}")
        else:
            raise _StepFailed(f"cannot connect to Postgres in {self.ref.container_name} as {user}: {last_error}")
        self.state = self._query_state(container)
        owner = self.state.get("owner")
        if self.owner_hint and not _ROLE_RE.match(self.owner_hint):
            self.owner_hint = None
        if not self.state.get("superuser") and not (self.state.get("createdb") and owner == self.state.get("user")):
            raise _StepFailed(
                f"the role {user} cannot drop and re-create {self.ref.db_name} "
                "(it needs to be a superuser, or own the database and have CREATEDB)"
            )
        sessions = int(self.state.get("sessions") or 0)
        detail = f"psql connects as {user}"
        if not self.exists:
            detail += f"; {self.ref.db_name} does not exist yet and will be created"
        elif sessions:
            detail += f"; {sessions} open session(s) will be disconnected"
        return detail

    def load_command(self) -> tuple[list[str], dict[str, str]]:
        return (
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", self.user, "-d", self.maintenance_db],
            dict(self.env),
        )

    def prelude(self) -> bytes:
        return postgres_prelude(self.ref.db_name, self.exists)

    def check_dump(self, prepared) -> str:
        if prepared.database is None:
            raise _StepFailed("could not tell which database the dump creates (no \\connect line)")
        if prepared.database != self.ref.db_name:
            raise _StepFailed(
                f"the dump re-creates database {prepared.database!r}, not {self.ref.db_name!r}; refusing to load it"
            )
        return f"creates {prepared.database}; {prepared.create_tables} tables"

    def unchanged(self, container) -> bool | None:
        """True when the original database is still there (the dump's DROP never ran)."""
        try:
            state = self._query_state(container)
        except Exception:
            return None
        if not self.exists:
            return not state.get("exists")
        return bool(state.get("exists")) and state.get("oid") == self.state.get("oid")

    def after_load(self, container, loaded: bool) -> list[str]:
        warnings: list[str] = []
        try:
            state = self._query_state(container)
        except Exception as exc:
            return [f"could not check the database after loading: {exc}"]
        if state.get("exists") and not state.get("allowconn"):
            code, out, err = self._psql(
                container,
                self.maintenance_db,
                f"ALTER DATABASE {pg_ident(self.ref.db_name)} WITH ALLOW_CONNECTIONS true",
            )
            if code != 0:
                warnings.append(
                    f"{self.ref.db_name} still refuses new connections; run ALTER DATABASE ... "
                    f"WITH ALLOW_CONNECTIONS true: {_clip(err or out, 300)}"
                )
        owner = self.original_owner
        if loaded and state.get("exists") and owner and owner != state.get("user") and owner != state.get("owner"):
            if "$arkive$" in owner:
                warnings.append(f"ownership was not given back to {owner} (unsupported role name)")
            else:
                code, out, err = self._psql(
                    container,
                    self.ref.db_name,
                    postgres_owner_sql(self.ref.db_name, owner, int(self.state.get("version") or 0)),
                )
                if code != 0:
                    warnings.append(
                        f"the restored objects belong to {state.get('user')}, not {owner} as before: "
                        f"{_clip(err or out, 300)}"
                    )
        return warnings

    def count_tables(self, container) -> int | None:
        code, out, _err = self._psql(container, self.ref.db_name, _PG_COUNT_SQL)
        numbers = re.findall(r"^\s*(\d+)\s*$", out, re.MULTILINE)
        return int(numbers[-1]) if code == 0 and numbers else None


class _MySQLDriver:
    db_type = "mariadb"
    original_owner = None

    def __init__(self, ref: DatabaseRef, owner_hint: str | None = None):
        self.ref = ref
        self.user = "root"
        self.env: dict[str, str] = {}
        self.client = "mysql"
        self.exists = False

    @property
    def tool(self) -> str:
        return self.client

    def _query(self, container, sql: str) -> tuple[int | None, str, str]:
        return _exec(container, [self.client, "-u", self.user, "-N", "-B", "-e", sql], self.env)

    def preflight(self, container) -> str:
        user, password = mysql_credentials(_container_env(container))
        if not _ROLE_RE.match(user):
            raise _StepFailed(f"the MySQL user {user!r} from the container environment is not supported")
        self.user = user
        self.env = {"MYSQL_PWD": password} if password else {}
        for client in ("mariadb", "mysql"):
            code, _out, _err = _exec(container, [client, "--version"])
            if code == 0:
                self.client = client
                break
        else:
            raise _StepFailed(f"no mariadb or mysql client found in {self.ref.container_name}")
        code, out, err = self._query(
            container,
            "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = " + mysql_literal(self.ref.db_name),  # nosec B608 - name validated and quoted by mysql_literal
        )
        numbers = re.findall(r"^\s*(\d+)\s*$", out, re.MULTILINE)
        if code != 0 or not numbers:
            raise _StepFailed(
                f"cannot query {self.ref.container_name} as {user}: {_clip(err or out) or f'exit {code}'}"
            )
        self.exists = int(numbers[-1]) > 0
        detail = f"{self.client} connects as {user}"
        if not self.exists:
            detail += f"; {self.ref.db_name} does not exist yet and will be created"
        return detail

    def load_command(self) -> tuple[list[str], dict[str, str]]:
        return [self.client, "-u", self.user], dict(self.env)

    def prelude(self) -> bytes:
        return mysql_prelude(self.ref.db_name, self.exists)

    def check_dump(self, prepared) -> str:
        names = mysql_dump_databases(prepared.path)
        if names != {self.ref.db_name}:
            found = ", ".join(sorted(names)) or "none"
            raise _StepFailed(f"the dump is for database(s) {found}, not {self.ref.db_name}; refusing to load it")
        return f"restores {self.ref.db_name}; {prepared.create_tables} tables"

    def unchanged(self, container) -> bool | None:
        return None  # tables are dropped first; a failed load may have changed it

    def after_load(self, container, loaded: bool) -> list[str]:
        return []

    def count_tables(self, container) -> int | None:
        code, out, _err = self._query(
            container,
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = "  # nosec B608 - name validated and quoted by mysql_literal
            + mysql_literal(self.ref.db_name)
            + " AND TABLE_TYPE IN ('BASE TABLE', 'SYSTEM VERSIONED')",
        )
        numbers = re.findall(r"^\s*(\d+)\s*$", out, re.MULTILINE)
        return int(numbers[-1]) if code == 0 and numbers else None


def _driver_for(ref: DatabaseRef, owner_hint: str | None = None):
    return _PostgresDriver(ref, owner_hint) if ref.db_type == "postgres" else _MySQLDriver(ref, owner_hint)


# ---------------------------------------------------------------------------
# Operation state
# ---------------------------------------------------------------------------


@dataclass
class DumpLocation:
    file: str
    size_bytes: int
    created_at: str | None
    source: str  # manifest | file_name


@dataclass
class _Operation:
    id: str
    kind: str  # restore | undo
    method: str
    ref: DatabaseRef
    work_dir: Path
    parent_id: str | None = None
    target: dict | None = None
    target_id: str | None = None
    snapshot_id: str | None = None
    snapshot_full_id: str | None = None
    dump_file: str | None = None
    dump_size_bytes: int = 0
    source_path: Path | None = None
    safety_dump_path: str | None = None
    safety_dump_size_bytes: int = 0
    database_changed: bool | None = None
    stop_containers: list[str] = field(default_factory=list)
    stopped_containers: list[str] = field(default_factory=list)
    # Undo: apps the restore being undone left stopped; started again with the others.
    adopt_stopped: list[str] = field(default_factory=list)
    # Postgres: owner of the database before anything was changed (given back after a load).
    db_owner: str | None = None
    steps: list[dict] = field(default_factory=list)
    instructions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bytes_done: int = 0
    bytes_total: int = 0
    status: str = STATUS_RUNNING
    phase: str | None = None
    error_message: str | None = None
    started_at: str = field(default_factory=_now)
    started_monotonic: float = field(default_factory=time.monotonic)

    def step(self, key: str) -> dict:
        return next(s for s in self.steps if s["key"] == key)


_ROW_JSON_COLUMNS = ("stop_containers", "stopped_containers", "steps", "instructions", "warnings")


class GuidedRestoreEngine:
    """Plans, runs and records guided database restores (one at a time)."""

    retention_days = 14
    # Head-room always left free in the restore dir (it usually shares a disk with appdata).
    min_free_bytes = 1024 * MIB
    container_stop_timeout = 60
    progress_interval = 2.0

    def __init__(
        self,
        config: ArkiveConfig,
        backup_engine=None,
        docker_client=None,
        lock_manager: BackupLockManager | None = None,
        dumper_factory: Callable[[], Any] | None = None,
    ):
        self.config = config
        self.backup_engine = backup_engine
        self.docker_client = docker_client
        self.lock_manager = lock_manager or BackupLockManager(config.config_dir)
        self._dumper_factory = dumper_factory
        self._running_id: str | None = None
        self._task: asyncio.Task | None = None

    # ---- public API ----

    @property
    def running_id(self) -> str | None:
        return self._running_id

    async def options(self) -> list[dict]:
        """Discovered databases with how each would be restored."""
        rows = await self._discovered_rows()
        databases: list[dict] = []
        seen: set[tuple] = set()
        for row in rows:
            for entry in self._row_databases(row):
                ref = DatabaseRef(
                    container_name=str(entry.get("container_name") or row["name"]),
                    db_type=normalize_db_type(entry.get("db_type")),
                    db_name=str(entry.get("db_name") or ""),
                    host_path=entry.get("host_path"),
                )
                key = (ref.container_name, ref.db_type, ref.db_name, ref.host_path)
                if not ref.db_name or key in seen:
                    continue
                seen.add(key)
                method, reason = restore_method(ref.db_type)
                if method != METHOD_UNSUPPORTED:
                    try:
                        validate_ref(ref)
                    except GuidedRestoreError as exc:
                        method, reason = METHOD_UNSUPPORTED, str(exc)
                status = next((r["status"] for r in rows if r["name"] == ref.container_name), None)
                databases.append(
                    {
                        "container_name": ref.container_name,
                        "db_type": ref.db_type,
                        "db_name": ref.db_name,
                        "host_path": ref.host_path,
                        "container_status": status,
                        "method": method,
                        "reason": reason,
                        "dependents": self._dependents(ref, rows),
                    }
                )
        databases.sort(key=lambda d: (d["method"] == METHOD_UNSUPPORTED, d["container_name"], d["db_name"]))
        return databases

    async def preview(self, selection) -> dict:
        """Everything a restore would do, checked without changing anything."""
        ref, method = await self._resolve(selection)
        target, snapshot = await self._resolve_snapshot(selection)
        self._require_backup_engine()
        rows = await self._discovered_rows()
        dependents = self._dependents(ref, rows)
        blockers: list[str] = []
        container_detail = None
        if method == METHOD_LOAD:
            self._require_docker()
            try:
                container_detail = await asyncio.to_thread(self._check_container_blocking, ref)
            except _StepFailed as exc:
                blockers.append(str(exc))
            except Exception as exc:
                if not _is_docker_unavailable(exc):
                    raise
                raise GuidedRestoreError(f"Docker is not reachable: {exc}", 503) from exc
            dependents = await asyncio.to_thread(self._live_status, dependents)

        scratch = Path(await asyncio.to_thread(self._preview_scratch_dir))
        try:
            try:
                location = await self._locate(target, snapshot, ref, scratch)
            except _StepFailed as exc:
                raise GuidedRestoreError(str(exc), 422) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)

        warnings: list[str] = []
        free = await asyncio.to_thread(lambda: shutil.disk_usage(self.config.restore_dir).free)
        needed = location.size_bytes * 2 + self.min_free_bytes
        if free < needed:
            blockers.append(
                f"not enough free space in {self.config.restore_dir} ({_mb(free)} MB free, ~{_mb(needed)} MB needed "
                "for the dump and a safety dump)"
            )
        if self.lock_manager.is_backup_running() or self.lock_manager.is_restore_running():
            warnings.append("A backup or restore is running right now; start the restore when it has finished.")
        if method == METHOD_LOAD and any(d.get("status") == "running" for d in dependents):
            warnings.append(
                "Apps that use this database are running. Stop them during the restore (selected below) so they "
                "don't write to the database while it is replaced or run migrations against a half-loaded one."
            )
        steps = _LOAD_STEPS if method == METHOD_LOAD else _MANUAL_STEPS
        return {
            "method": method,
            "database": {
                "container_name": ref.container_name,
                "db_type": ref.db_type,
                "db_name": ref.db_name,
                "host_path": ref.host_path,
                "container_detail": container_detail,
            },
            "snapshot": {
                "id": snapshot["id"],
                "full_id": snapshot["full_id"],
                "time": snapshot.get("time"),
                "target_id": target["id"],
                "target_name": target.get("name") or target["id"],
            },
            "dump": {
                "file": location.file,
                "size_bytes": location.size_bytes,
                "created_at": location.created_at,
                "source": location.source,
            },
            "dependents": dependents,
            "confirm_phrase": ref.db_name if method == METHOD_LOAD else None,
            "steps": [{"key": key, "label": _STEP_LABELS[key]} for key in steps],
            "blockers": blockers,
            "warnings": warnings,
            "safety_dump_retention_days": self.retention_days,
        }

    async def start(self, body) -> dict:
        """Validate, take the backup lock and run the restore in the background."""
        ref, method = await self._resolve(body)
        if method == METHOD_LOAD and body.confirm != ref.db_name:
            raise GuidedRestoreError(f"Type the database name ({ref.db_name}) exactly to confirm the restore")
        target, snapshot = await self._resolve_snapshot(body)
        self._require_backup_engine()
        stop: list[str] = []
        if method == METHOD_LOAD:
            self._require_docker()
            stop = await self._validate_stop_list(ref, body.stop_containers)
        op_id = uuid.uuid4().hex
        op = _Operation(
            id=op_id,
            kind="restore",
            method=method,
            ref=ref,
            work_dir=Path(self.config.restore_dir) / f"{WORK_DIR_PREFIX}{op_id}",
            target=target,
            target_id=target["id"],
            snapshot_id=snapshot["id"],
            snapshot_full_id=snapshot["full_id"],
            stop_containers=stop,
        )
        op.steps = self._new_steps(_LOAD_STEPS if method == METHOD_LOAD else _MANUAL_STEPS)
        return await self._launch(op)

    async def start_undo(self, op_id: str, body) -> dict:
        """Load the safety dump of a previous restore back into the database."""
        parent = await self._row(op_id)
        if parent is None:
            raise GuidedRestoreError("Restore not found", 404)
        if parent["method"] != METHOD_LOAD:
            raise GuidedRestoreError("Only restores that loaded a database can be undone")
        if parent["status"] == STATUS_RUNNING:
            raise GuidedRestoreError("That restore is still running", 409)
        if parent["database_changed"] == 0:
            raise GuidedRestoreError("That restore did not change the database; there is nothing to undo")
        safety = self._owned_safety_dump(parent)
        if safety is None:
            raise GuidedRestoreError(
                f"The safety dump of that restore is no longer available (it is kept for {self.retention_days} days)",
                410,
            )
        ref = DatabaseRef(
            container_name=parent["container_name"],
            db_type=parent["db_type"],
            db_name=parent["db_name"],
            host_path=parent["host_path"],
        )
        validate_ref(ref)
        if body.confirm != ref.db_name:
            raise GuidedRestoreError(f"Type the database name ({ref.db_name}) exactly to confirm the undo")
        self._require_docker()
        requested = body.stop_containers if body.stop_containers is not None else parent["stop_containers"]
        stop = await self._validate_stop_list(ref, requested, also_allowed=parent["stop_containers"])
        op_id = uuid.uuid4().hex
        op = _Operation(
            id=op_id,
            kind="undo",
            method=METHOD_LOAD,
            ref=ref,
            work_dir=Path(self.config.restore_dir) / f"{WORK_DIR_PREFIX}{op_id}",
            parent_id=parent["id"],
            dump_file=safety.name,
            dump_size_bytes=int(parent.get("safety_dump_size_bytes") or 0),
            source_path=safety,
            stop_containers=stop,
            adopt_stopped=list(parent["stopped_containers"]),
            db_owner=parent.get("db_owner"),
        )
        op.steps = self._new_steps(_UNDO_STEPS)
        op.step("verify_dump")["label"] = "Check the safety dump is complete"
        return await self._launch(op)

    async def get(self, op_id: str) -> dict | None:
        row = await self._row(op_id)
        return self._public(row) if row else None

    async def history(self, limit: int = 20, offset: int = 0) -> dict:
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    self._SELECT + " ORDER BY g.started_at DESC, g.rowid DESC LIMIT ? OFFSET ?", (limit, offset)
                )
                rows = [self._decode(r) for r in await cursor.fetchall()]
                cursor = await db.execute("SELECT COUNT(*) FROM guided_restores")
                total = (await cursor.fetchone())[0]
        except aiosqlite.OperationalError:
            return {"items": [], "total": 0}
        return {"items": [self._public(r) for r in rows], "total": total}

    async def recover_after_restart(self) -> None:
        """Startup: fail operations a crash or restart interrupted, saying what may be left behind."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, phase, method, db_name, stopped_containers, steps FROM guided_restores "
                    "WHERE status = 'running'"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    message = "Interrupted by a restart of Arkive"
                    if row["phase"] == "load":
                        message += f" while loading; {row['db_name']} may be partially restored (use Undo)"
                    stopped = json.loads(row["stopped_containers"] or "[]")
                    if stopped:
                        message += (
                            f". These apps were stopped by the restore and may still be stopped: {', '.join(stopped)}"
                        )
                    steps = json.loads(row["steps"] or "[]")
                    for step in steps:
                        if step.get("status") in ("pending", "running"):
                            step["status"] = "failed" if step.get("status") == "running" else "skipped"
                    await db.execute(
                        "UPDATE guided_restores SET status = 'failed', error_message = ?, completed_at = ?, "
                        "steps = ?, database_changed = CASE WHEN phase = 'load' THEN 1 ELSE database_changed END "
                        "WHERE id = ?",
                        (message, _now(), json.dumps(steps), row["id"]),
                    )
                await db.commit()
                if rows:
                    logger.warning("Marked %d interrupted guided restore(s) as failed", len(rows))
        except aiosqlite.OperationalError as exc:
            logger.debug("Guided restore recovery skipped: %s", exc)
        except Exception as exc:
            logger.warning("Could not recover interrupted guided restores: %s", exc)

    # ---- validation / lookup ----

    async def _resolve(self, selection) -> tuple[DatabaseRef, str]:
        rows = await self._discovered_rows()
        db_type = normalize_db_type(selection.db_type)
        wanted_path = os.path.normpath(selection.host_path) if selection.host_path else None
        for row in rows:
            for entry in self._row_databases(row):
                if str(entry.get("container_name") or row["name"]) != selection.container_name:
                    continue
                if normalize_db_type(entry.get("db_type")) != db_type or entry.get("db_name") != selection.db_name:
                    continue
                path = entry.get("host_path")
                if db_type == "sqlite" and (os.path.normpath(path) if path else None) != wanted_path:
                    continue
                ref = DatabaseRef(selection.container_name, db_type, selection.db_name, path)
                method, reason = restore_method(db_type)
                if method == METHOD_UNSUPPORTED:
                    raise GuidedRestoreError(reason or "Unsupported database type", 422)
                validate_ref(ref)
                return ref, method
        raise GuidedRestoreError(
            f"{selection.container_name}/{selection.db_name} is not a discovered database; run a discovery scan", 404
        )

    async def _resolve_snapshot(self, selection) -> tuple[dict, dict]:
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM storage_targets WHERE id = ?", (selection.target_id,))
            target = await cursor.fetchone()
            if target is None:
                raise GuidedRestoreError("Storage target not found", 404)
            cursor = await db.execute(
                "SELECT * FROM snapshots WHERE target_id = ? AND (id = ? OR full_id = ?)",
                (selection.target_id, selection.snapshot_id, selection.snapshot_id),
            )
            snapshot = await cursor.fetchone()
        if snapshot is None:
            raise GuidedRestoreError("Snapshot not found on that target; refresh the snapshot list", 404)
        target = dict(target)
        try:
            target["config"] = decrypt_config(target.get("config") or "{}", str(self.config.config_dir))
        except Exception as exc:
            raise GuidedRestoreError(f"Cannot read the target's settings: {exc}", 500) from exc
        snapshot = dict(snapshot)
        try:
            snapshot["paths"] = json.loads(snapshot.get("paths") or "[]")
        except (TypeError, ValueError):
            snapshot["paths"] = []
        if not self._covers_dump_dir(snapshot["paths"]):
            raise GuidedRestoreError(
                f"Snapshot {snapshot['id']} does not include the database dumps folder ({self.config.dump_dir})", 422
            )
        return target, snapshot

    def _covers_dump_dir(self, paths: list) -> bool:
        dump_dir = os.path.normpath(str(self.config.dump_dir))
        for path in paths or []:
            norm = os.path.normpath(str(path))
            if norm == dump_dir or dump_dir.startswith(norm.rstrip("/") + "/"):
                return True
        return False

    async def _validate_stop_list(self, ref: DatabaseRef, names: list[str], also_allowed: list[str] = ()) -> list[str]:
        if not names:
            return []
        rows = await self._discovered_rows()
        allowed = {d["name"] for d in self._dependents(ref, rows)} | set(also_allowed or ())
        bad = [n for n in names if n not in allowed or n == ref.container_name]
        if bad:
            raise GuidedRestoreError(
                f"Only apps that use {ref.label} can be stopped during the restore: {', '.join(bad)} is not one"
            )
        return list(names)

    def _require_docker(self) -> None:
        if self.docker_client is None:
            raise GuidedRestoreError("Docker is not available, so the database cannot be loaded", 503)

    def _require_backup_engine(self) -> None:
        if self.backup_engine is None:
            raise GuidedRestoreError("The backup engine is not available", 503)

    async def _discovered_rows(self) -> list[dict]:
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT name, status, databases, compose_project FROM discovered_containers")
            return [dict(r) for r in await cursor.fetchall()]

    @staticmethod
    def _row_databases(row: dict) -> list[dict]:
        try:
            entries = json.loads(row.get("databases") or "[]")
        except (TypeError, ValueError):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def _dependents(self, ref: DatabaseRef, rows: list[dict]) -> list[dict]:
        """Containers that use the database: they reference it, or share its compose project."""
        own = next((r for r in rows if r["name"] == ref.container_name), None)
        project = (own or {}).get("compose_project")
        result = []
        for row in rows:
            if row["name"] == ref.container_name:
                continue
            reasons = []
            if any(str(e.get("container_name") or "") == ref.container_name for e in self._row_databases(row)):
                reasons.append("uses this database")
            if project and row.get("compose_project") == project:
                reasons.append(f"same compose project ({project})")
            if reasons:
                result.append({"name": row["name"], "status": row.get("status"), "reason": "; ".join(reasons)})
        return result

    def _live_status(self, dependents: list[dict]) -> list[dict]:
        for dep in dependents:
            try:
                dep["status"] = getattr(self.docker_client.containers.get(dep["name"]), "status", dep.get("status"))
            except Exception as exc:
                if _is_docker_unavailable(exc):
                    break
                dep["status"] = "missing"
        return dependents

    # ---- launching ----

    @staticmethod
    def _new_steps(keys) -> list[dict]:
        return [{"key": k, "label": _STEP_LABELS[k], "status": "pending", "detail": None} for k in keys]

    async def _launch(self, op: _Operation) -> dict:
        if self._running_id is not None:
            raise GuidedRestoreError("A guided restore is already running", 409)
        await asyncio.to_thread(self._prune_work_dirs)
        if not self.lock_manager.acquire_backup_lock(f"guided-restore-{op.id}"):
            raise GuidedRestoreError(
                f"{self.lock_manager.lock_conflict_message()}; try again when it has finished", 409
            )
        self._running_id = op.id
        try:
            await asyncio.to_thread(self._create_work_dir, op)
            await self._insert(op)
        except BaseException:
            self._running_id = None
            self.lock_manager.release_backup_lock()
            await asyncio.to_thread(shutil.rmtree, op.work_dir, True)
            raise
        self._task = asyncio.create_task(self._run(op))
        return {"id": op.id, "status": STATUS_RUNNING}

    def _preview_scratch_dir(self) -> str:
        root = Path(self.config.restore_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = tempfile.mkdtemp(prefix=PREVIEW_DIR_PREFIX, dir=str(root))
        (Path(path) / WORK_MARKER).write_text(PREVIEW_MARKER)
        return path

    @staticmethod
    def _create_work_dir(op: _Operation) -> None:
        op.work_dir.parent.mkdir(parents=True, exist_ok=True)
        op.work_dir.mkdir()  # never adopt an existing directory
        (op.work_dir / WORK_MARKER).write_text(op.id)

    # ---- running ----

    async def _run(self, op: _Operation) -> None:
        try:
            if op.method == METHOD_MANUAL:
                await self._run_manual(op)
            else:
                await self._run_load(op)
        except Exception as exc:  # last resort: never leave a row 'running'
            logger.exception("Guided restore %s crashed", op.id)
            op.status = STATUS_FAILED
            op.error_message = op.error_message or f"Unexpected error: {_clip(str(exc), 800)}"
        finally:
            try:
                await self._finish(op)
            finally:
                self.lock_manager.release_backup_lock()
                if self._running_id == op.id:
                    self._running_id = None

    async def _set_step(self, op: _Operation, key: str, status: str, detail: str | None = None) -> None:
        step = op.step(key)
        step["status"] = status
        if detail is not None:
            step["detail"] = detail[:1000]
        if status == "running":
            op.phase = key
        await self._save(op)

    async def _run_step(self, op: _Operation, key: str, func, *args):
        """Run one step: mark it running, then done with the detail it returns, or failed."""
        await self._set_step(op, key, "running")
        try:
            detail = await func(op, *args)
        except _StepSkipped as exc:
            await self._set_step(op, key, "skipped", str(exc))
            return str(exc)
        except _StepFailed as exc:
            await self._set_step(op, key, "failed", str(exc))
            raise
        except Exception as exc:
            if _is_docker_unavailable(exc):
                message = f"Docker is not reachable: {exc}"
            else:
                logger.exception("Guided restore %s: step %s errored", op.id, key)
                message = f"unexpected error: {_clip(str(exc), 400)}"
            await self._set_step(op, key, "failed", message)
            raise _StepFailed(message) from exc
        await self._set_step(op, key, "done", detail)
        return detail

    async def _run_load(self, op: _Operation) -> None:
        driver = _driver_for(op.ref, op.db_owner if op.kind == "undo" else None)
        prepared = None
        try:
            await self._run_step(op, "check_container", self._step_check_container, driver)
            if op.kind == "restore":
                await self._run_step(op, "locate_dump", self._step_locate)
                await self._run_step(op, "restore_dump", self._step_restore)
            holder: dict = {}
            await self._run_step(op, "verify_dump", self._step_verify_sql_dump, driver, holder)
            prepared = holder["prepared"]
            try:
                await self._run_step(op, "safety_dump", self._step_safety_dump, driver)
            except _StepFailed as exc:
                if op.kind != "undo":
                    raise
                # Undo is how a broken restore gets repaired; a database too broken
                # to dump must not block it. (A restore never runs without one.)
                op.warnings.append(f"No copy of the database as it was before the undo was kept: {exc}")
            await self._run_step(op, "stop_apps", self._step_stop_apps)
            await self._run_step(op, "load", self._step_load, driver, prepared)
            # The data is in: from here on a problem is a warning, not a failed restore.
            for key, func, args in (
                ("verify", self._step_verify, (driver, prepared)),
                ("start_apps", self._step_start_apps, ()),
            ):
                try:
                    await self._run_step(op, key, func, *args)
                except _StepFailed as exc:
                    op.warnings.append(f"{_STEP_LABELS[key]}: {exc}")
            op.status = STATUS_WARNINGS if op.warnings else STATUS_COMPLETED
        except _StepFailed as exc:
            op.status = STATUS_FAILED
            op.error_message = _sentence(f"{_STEP_LABELS.get(op.phase or '', 'Restore')} failed: {exc}"[:1000])
            await self._after_failure(op)
        finally:
            await asyncio.to_thread(self._discard_restored_copy, op, prepared)

    async def _after_failure(self, op: _Operation) -> None:
        for step in op.steps:
            if step["status"] == "pending":
                step["status"] = "skipped"
        if op.database_changed is True:
            what = "the restore" if op.kind == "restore" else "the undo"
            op.error_message += (
                f" {op.ref.db_name} may be partially restored. Use Undo to load the safety dump taken before {what}."
            )
        elif op.database_changed is False:
            op.error_message += f" {op.ref.db_name} was not changed."
        if not op.stopped_containers:
            return
        start = op.step("start_apps")
        if op.database_changed is True:
            start["status"] = "skipped"
            start["detail"] = (
                f"left stopped because the database may be incomplete: {', '.join(op.stopped_containers)}. "
                "Undo starts them again, or start them yourself once the database is fixed."
            )
            op.error_message += f" Apps left stopped: {', '.join(op.stopped_containers)}."
            return
        try:
            await self._run_step(op, "start_apps", self._step_start_apps)
        except _StepFailed:
            pass

    async def _run_manual(self, op: _Operation) -> None:
        try:
            await self._run_step(op, "locate_dump", self._step_locate)
            await self._run_step(op, "restore_dump", self._step_restore)
            await self._run_step(op, "verify_dump", self._step_verify_sqlite)
            try:
                await self._run_step(op, "safety_dump", self._step_safety_dump, None)
            except _StepFailed as exc:
                # Nothing is overwritten by Arkive here; the steps keep a copy too.
                op.warnings.append(f"No safety copy of the current database was taken: {exc}")
            await self._run_step(op, "instructions", self._step_instructions)
            op.status = STATUS_ACTION_REQUIRED
        except _StepFailed as exc:
            op.status = STATUS_FAILED
            op.error_message = _sentence(f"{_STEP_LABELS.get(op.phase or '', 'Restore')} failed: {exc}"[:1000])
            for step in op.steps:
                if step["status"] == "pending":
                    step["status"] = "skipped"

    # ---- steps ----

    def _docker(self):
        if self.docker_client is None:
            raise _StepFailed("Docker is not available")
        return self.docker_client

    def _get_container(self, name: str):
        try:
            return self._docker().containers.get(name)
        except Exception as exc:
            if _is_docker_unavailable(exc):
                raise
            raise _StepFailed(f"container {name} not found: {exc}") from exc

    def _check_container_blocking(self, ref: DatabaseRef, driver=None) -> str:
        container = self._get_container(ref.container_name)
        status = getattr(container, "status", None)
        if status != "running":
            raise _StepFailed(f"{ref.container_name} is {status or 'not running'}; start it first")
        return (driver or _driver_for(ref)).preflight(container)

    async def _step_check_container(self, op: _Operation, driver) -> str:
        detail = await asyncio.to_thread(self._check_container_blocking, op.ref, driver)
        op.db_owner = driver.original_owner
        return detail

    async def _step_locate(self, op: _Operation) -> str:
        snapshot = {"id": op.snapshot_id, "full_id": op.snapshot_full_id}
        location = await self._locate(op.target, snapshot, op.ref, op.work_dir / "manifest")
        op.dump_file = location.file
        op.dump_size_bytes = location.size_bytes
        when = f", dumped {location.created_at}" if location.created_at else ""
        return f"{location.file} ({_size(location.size_bytes)}{when})"

    async def _step_restore(self, op: _Operation) -> str:
        await self._require_free_space(op.dump_size_bytes, "restore the dump")
        dump_dir = str(self.config.dump_dir).rstrip("/")
        restore_to = op.work_dir / "snapshot"
        result = await self.backup_engine.restore(
            target=op.target,
            snapshot_id=op.snapshot_full_id,
            paths=[f"{dump_dir}/{op.dump_file}"],
            restore_to=str(restore_to),
        )
        if result.get("status") != "success":
            raise _StepFailed(f"restic restore failed: {result.get('error') or 'restic returned an error'}")
        restored = restore_to / dump_dir.lstrip("/") / op.dump_file
        if not await asyncio.to_thread(restored.is_file):
            raise _StepFailed("the dump file is missing after the restore")
        op.source_path = restored
        target_name = (op.target or {}).get("name") or op.target_id
        return f"restored from snapshot {op.snapshot_id} on {target_name}"

    async def _step_verify_sql_dump(self, op: _Operation, driver, holder: dict) -> str:
        source = op.source_path
        if source is None or not await asyncio.to_thread(source.is_file):
            raise _StepFailed("the dump file is missing")
        dest = op.work_dir / "load.sql"
        if op.kind == "undo":
            # Never consume the safety dump itself: undo must stay repeatable.
            copy = op.work_dir / ("undo-source.sql.gz" if source.name.endswith(".gz") else "undo-source.sql")
            await asyncio.to_thread(shutil.copyfile, source, copy)
            source = copy
        try:
            prepared = await asyncio.to_thread(prepare_sql_dump, source, dest, op.ref.db_type, self.min_free_bytes)
        except DrillSkip as exc:
            raise _StepFailed(str(exc)) from exc
        except DrillFailure as exc:
            raise _StepFailed(str(exc)) from exc
        if not prepared.trailer_ok:
            raise _StepFailed("the dump is truncated (its completion trailer is missing)")
        detail = await asyncio.to_thread(driver.check_dump, prepared)
        holder["prepared"] = prepared
        op.bytes_total = prepared.size_bytes
        return f"complete; {detail}"

    async def _step_verify_sqlite(self, op: _Operation) -> str:
        source = op.source_path
        if source is None:
            raise _StepFailed("the dump file is missing")
        restored_dir = op.work_dir / "restored"
        dest = restored_dir / source.name

        def _move() -> None:
            restored_dir.mkdir(exist_ok=True)
            os.replace(source, dest)
            shutil.rmtree(op.work_dir / "snapshot", ignore_errors=True)

        await asyncio.to_thread(_move)
        op.source_path = dest
        status, detail = await asyncio.to_thread(verify_sqlite_file, dest)
        if status != "passed":
            raise _StepFailed(f"the restored database is not usable: {detail}")
        return detail

    async def _step_safety_dump(self, op: _Operation, driver) -> str:
        if driver is not None and not driver.exists:
            raise _StepSkipped(f"{op.ref.db_name} does not exist yet; nothing to keep")
        await self._require_free_space(op.dump_size_bytes or op.bytes_total, "take a safety dump")
        dumper = self._dumper_factory() if self._dumper_factory else DBDumper(self.docker_client, self.config)
        result = await dumper.dump_to_directory(op.ref.discovered(), op.work_dir / "safety")
        if result.status != "success" or not result.dump_path:
            raise _StepFailed(
                f"could not dump the current database ({result.error or 'dump failed'}); nothing was changed"
            )
        op.safety_dump_path = result.dump_path
        op.safety_dump_size_bytes = result.dump_size_bytes
        return (
            f"{os.path.basename(result.dump_path)} ({_size(result.dump_size_bytes)}), kept {self.retention_days} days"
        )

    async def _step_stop_apps(self, op: _Operation) -> str:
        if not op.stop_containers:
            raise _StepSkipped("none selected")
        return await asyncio.to_thread(self._stop_apps_blocking, op)

    def _stop_apps_blocking(self, op: _Operation) -> str:
        already, adopted = [], []
        for name in op.stop_containers:
            container = self._get_container(name)
            try:
                container.reload()
            except Exception:
                pass
            if getattr(container, "status", None) != "running":
                if name in op.adopt_stopped:
                    adopted.append(name)
                    op.stopped_containers.append(name)
                else:
                    already.append(name)
                continue
            try:
                container.stop(timeout=self.container_stop_timeout)
            except Exception as exc:
                if _is_docker_unavailable(exc):
                    raise
                raise _StepFailed(f"could not stop {name}: {exc}") from exc
            op.stopped_containers.append(name)
        stopped_now = [n for n in op.stopped_containers if n not in adopted]
        detail = f"stopped {', '.join(stopped_now)}" if stopped_now else "nothing to stop"
        if adopted:
            detail += f" ({', '.join(adopted)} still stopped by the restore being undone)"
        if already:
            detail += f" ({', '.join(already)} already stopped)"
        return detail

    async def _step_load(self, op: _Operation, driver, prepared) -> str:
        op.database_changed = True  # pessimistic from the first byte on
        op.bytes_done = 0
        prelude = driver.prelude()
        op.bytes_total = prepared.size_bytes + len(prelude)

        def progress(count: int) -> None:
            op.bytes_done += count

        started = time.monotonic()
        future = asyncio.ensure_future(
            asyncio.to_thread(self._load_blocking, op, driver, prelude, prepared.path, progress)
        )
        while not future.done():
            await asyncio.wait({future}, timeout=self.progress_interval)
            await self._save(op)
        try:
            result: StdinExecResult = future.result()
        except Exception:
            if op.bytes_done == 0:
                op.database_changed = False  # failed before a single byte reached the client
            await asyncio.to_thread(self._after_load_blocking, op, driver, False)
            raise
        seconds = int(time.monotonic() - started)
        if result.exit_code == 0 and result.write_error:
            # Exited "successfully" without reading the whole dump: not a complete restore.
            await asyncio.to_thread(self._after_load_blocking, op, driver, False)
            raise _StepFailed(f"{driver.tool} stopped reading the dump before its end ({result.write_error})")
        if result.exit_code != 0:
            unchanged = await asyncio.to_thread(self._unchanged_blocking, op, driver)
            if unchanged:
                op.database_changed = False
            await asyncio.to_thread(self._after_load_blocking, op, driver, False)
            errors = [ln for ln in result.stderr.splitlines() if ln.strip()]
            important = [ln for ln in errors if "ERROR" in ln or "error" in ln.lower()] or errors
            reason = " | ".join(important[-3:]) or result.write_error or "no error output"
            code = "unknown" if result.exit_code is None else result.exit_code
            raise _StepFailed(f"{driver.tool} exited with code {code}: {_clip(reason)}")
        warnings = await asyncio.to_thread(self._after_load_blocking, op, driver, True)
        op.warnings.extend(warnings)
        return f"{_size(prepared.size_bytes)} loaded with {driver.tool} in {seconds}s"

    def _load_blocking(self, op: _Operation, driver, prelude: bytes, path: Path, progress) -> StdinExecResult:
        container = self._get_container(op.ref.container_name)
        cmd, env = driver.load_command()
        chunks = itertools.chain([prelude], _file_chunks(path))
        return exec_with_stdin(container, cmd, env, chunks, progress)

    def _unchanged_blocking(self, op: _Operation, driver) -> bool | None:
        try:
            return driver.unchanged(self._get_container(op.ref.container_name))
        except Exception:
            return None

    def _after_load_blocking(self, op: _Operation, driver, loaded: bool) -> list[str]:
        try:
            return driver.after_load(self._get_container(op.ref.container_name), loaded)
        except Exception as exc:
            return [f"post-load checks failed: {exc}"]

    async def _step_verify(self, op: _Operation, driver, prepared) -> str:
        container = await asyncio.to_thread(self._get_container, op.ref.container_name)
        count = await asyncio.to_thread(driver.count_tables, container)
        expected = prepared.create_tables
        if count is None:
            op.warnings.append("Could not count the restored tables")
            return "could not count the restored tables"
        if count < expected:
            op.warnings.append(f"Only {count} of the {expected} tables in the dump are in the database")
            return f"{count} of {expected} tables present"
        extra = f" (the dump declares {expected})" if count != expected else ""
        return f"{count} tables in {op.ref.db_name}{extra}"

    async def _step_start_apps(self, op: _Operation) -> str:
        if not op.stopped_containers:
            raise _StepSkipped("nothing was stopped")
        return await asyncio.to_thread(self._start_apps_blocking, op)

    def _start_apps_blocking(self, op: _Operation) -> str:
        started, failed = [], []
        for name in op.stopped_containers:
            try:
                self._get_container(name).start()
                started.append(name)
            except Exception as exc:
                failed.append(f"{name} ({exc})")
        if failed:
            raise _StepFailed(f"could not start {', '.join(failed)}; start them from the Docker tab")
        return f"started {', '.join(started)}"

    async def _step_instructions(self, op: _Operation) -> str:
        restored = await asyncio.to_thread(self._host_path, str(op.source_path))
        safety = await asyncio.to_thread(self._host_path, op.safety_dump_path) if op.safety_dump_path else None
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        op.instructions = sqlite_instructions(op.ref, restored, safety, stamp)
        return "the restored file is ready; follow the steps below to put it in place"

    def _host_path(self, path: str) -> str:
        """Where ``path`` (inside Arkive's container) is on the server, when that can be told."""
        if not running_in_container() or self.docker_client is None:
            return path
        try:
            return host_path_for(path, self.docker_client) or path
        except Exception:
            return path

    async def _require_free_space(self, needed_bytes: int, action: str) -> None:
        def _free() -> int:
            self.config.restore_dir.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.config.restore_dir).free

        free = await asyncio.to_thread(_free)
        needed = int(needed_bytes or 0) + self.min_free_bytes
        if free < needed:
            raise _StepFailed(
                f"not enough free space in {self.config.restore_dir} to {action} "
                f"({_mb(free)} MB free, ~{_mb(needed)} MB needed)"
            )

    # ---- locating the dump in a snapshot ----

    async def _locate(self, target: dict, snapshot: dict, ref: DatabaseRef, scratch: Path) -> DumpLocation:
        dump_dir = str(self.config.dump_dir).rstrip("/")
        snapshot_id = snapshot.get("full_id") or snapshot["id"]
        try:
            entries = await self.backup_engine.ls(target, snapshot_id, dump_dir, strict=True)
        except ResticError as exc:
            raise _StepFailed(f"could not list {dump_dir} in snapshot {snapshot['id']}: {exc}") from exc
        except ValueError as exc:
            raise _StepFailed(str(exc)) from exc
        files = {
            e["name"]: e for e in entries or [] if e.get("type") == "file" and _SAFE_FILE_RE.match(str(e.get("name")))
        }
        if DUMP_MANIFEST_NAME in files:
            manifest = await self._read_manifest(target, snapshot_id, scratch)
            if manifest is not None:
                found = self._match_manifest(manifest, files, ref)
                if found:
                    return found
        found = self._match_file_names(files, ref)
        if found:
            return found
        raise _StepFailed(
            f"snapshot {snapshot['id']} has no dump of {ref.label} (it may predate the database, or its dump failed)"
        )

    async def _read_manifest(self, target: dict, snapshot_id: str, scratch: Path) -> dict | None:
        dump_dir = str(self.config.dump_dir).rstrip("/")
        restore_to = scratch / "manifest"
        try:
            await asyncio.to_thread(restore_to.mkdir, parents=True, exist_ok=True)
            result = await self.backup_engine.restore(
                target=target,
                snapshot_id=snapshot_id,
                paths=[f"{dump_dir}/{DUMP_MANIFEST_NAME}"],
                restore_to=str(restore_to),
            )
            if result.get("status") != "success":
                raise _StepFailed(f"could not restore the dump manifest: {result.get('error') or 'restic error'}")
            path = restore_to / dump_dir.lstrip("/") / DUMP_MANIFEST_NAME

            def _load() -> dict | None:
                if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
                    return None
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None
                return data if isinstance(data, dict) and isinstance(data.get("dumps"), list) else None

            return await asyncio.to_thread(_load)
        finally:
            await asyncio.to_thread(shutil.rmtree, restore_to, True)

    @staticmethod
    def _matches_type(name: str, db_type: str) -> bool:
        return any(name.endswith("." + ext) for ext in _DUMP_EXTENSIONS.get(db_type, ()))

    def _match_manifest(self, manifest: dict, files: dict[str, dict], ref: DatabaseRef) -> DumpLocation | None:
        best: tuple[str, DumpLocation] | None = None
        for entry in manifest.get("dumps") or []:
            if not isinstance(entry, dict):
                continue
            name = os.path.basename(str(entry.get("file") or ""))
            if name not in files or not self._matches_type(name, ref.db_type):
                continue
            if str(entry.get("kind") or "database") != "database":
                continue
            if str(entry.get("container_name") or "") != ref.container_name:
                continue
            if normalize_db_type(entry.get("db_type")) != ref.db_type or str(entry.get("db_name") or "") != ref.db_name:
                continue
            entry_path = entry.get("host_path")
            if ref.db_type == "sqlite" and entry_path and ref.host_path:
                if os.path.normpath(str(entry_path)) != os.path.normpath(ref.host_path):
                    continue
            size = files[name].get("size")
            created = str(entry.get("created_at") or "") or None
            location = DumpLocation(
                file=name,
                size_bytes=size if isinstance(size, int) else int(entry.get("size_bytes") or 0),
                created_at=created,
                source="manifest",
            )
            key = created or name
            if best is None or key > best[0]:
                best = (key, location)
        return best[1] if best else None

    def _match_file_names(self, files: dict[str, dict], ref: DatabaseRef) -> DumpLocation | None:
        """Snapshots without a manifest: match the dumper's file-name prefixes."""
        prefixes = {dump_prefix(ref.container_name, ref.db_type, ref.db_name, ref.host_path)}
        if ref.db_type != "sqlite":
            prefixes.add(legacy_dump_prefix(ref.container_name, ref.db_name))
        best: tuple[str, DumpLocation] | None = None
        for name, listing in files.items():
            match = _DUMP_FILE_RE.match(name)
            if not match or match.group("prefix") not in prefixes or not self._matches_type(name, ref.db_type):
                continue
            ts = match.group("ts")
            size = listing.get("size")
            created = datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            location = DumpLocation(
                file=name, size_bytes=size if isinstance(size, int) else 0, created_at=created, source="file_name"
            )
            if best is None or ts > best[0]:
                best = (ts, location)
        return best[1] if best else None

    # ---- cleanup / retention ----

    def _discard_restored_copy(self, op: _Operation, prepared) -> None:
        """The restored dump is in the repository anyway; keep only the safety dump."""
        if op.method != METHOD_LOAD:
            return
        shutil.rmtree(op.work_dir / "snapshot", ignore_errors=True)
        shutil.rmtree(op.work_dir / "manifest", ignore_errors=True)
        for name in ("load.sql", "undo-source.sql", "undo-source.sql.gz"):
            try:
                (op.work_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _owned_safety_dump(self, row: dict) -> Path | None:
        path = row.get("safety_dump_path")
        if not path:
            return None
        work = Path(self.config.restore_dir) / f"{WORK_DIR_PREFIX}{row['id']}"
        candidate = Path(path)
        try:
            candidate.relative_to(work)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        try:
            if (work / WORK_MARKER).read_text(encoding="utf-8").strip() != row["id"]:
                return None
        except OSError:
            return None
        return candidate

    def _prune_work_dirs(self) -> int:
        """Delete work dirs (safety dumps, restored SQLite files) older than the retention period."""
        root = Path(self.config.restore_dir)
        if not root.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for entry in root.iterdir():
            if entry.name.startswith(PREVIEW_DIR_PREFIX) and entry.is_dir() and not entry.is_symlink():
                try:
                    owned = (entry / WORK_MARKER).read_text(encoding="utf-8").strip() == PREVIEW_MARKER
                    stale = entry.stat().st_mtime < time.time() - 86400
                except OSError:
                    continue
                if owned and stale:
                    shutil.rmtree(entry, ignore_errors=True)
                continue
            match = _WORK_DIR_RE.match(entry.name)
            if not match or entry.is_symlink() or not entry.is_dir() or match.group(1) == self._running_id:
                continue
            try:
                if (entry / WORK_MARKER).read_text(encoding="utf-8").strip() != match.group(1):
                    continue
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            logger.info("Removed expired guided-restore files %s", entry)
        return removed

    # ---- persistence ----

    _SELECT = """
        SELECT g.*, t.name AS target_name FROM guided_restores g
        LEFT JOIN storage_targets t ON t.id = g.target_id
    """

    async def _insert(self, op: _Operation) -> None:
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                """INSERT INTO guided_restores
                   (id, kind, parent_id, status, method, container_name, db_type, db_name, host_path,
                    target_id, snapshot_id, dump_file, work_dir, stop_containers, steps, started_at)
                   VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    op.id,
                    op.kind,
                    op.parent_id,
                    op.method,
                    op.ref.container_name,
                    op.ref.db_type,
                    op.ref.db_name,
                    op.ref.host_path,
                    op.target_id,
                    op.snapshot_id,
                    op.dump_file,
                    str(op.work_dir),
                    json.dumps(op.stop_containers),
                    json.dumps(op.steps),
                    op.started_at,
                ),
            )
            what = "Undo of a database restore" if op.kind == "undo" else "Guided database restore"
            await log_activity(
                db,
                "restore",
                "database_restore_started",
                f"{what} started: {op.ref.label}",
                {"guided_restore_id": op.id, "kind": op.kind, "snapshot_id": op.snapshot_id},
            )

    async def _save(self, op: _Operation, final: bool = False) -> None:
        completed_at = _now() if final else None
        duration = int(time.monotonic() - op.started_monotonic) if final else None
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """UPDATE guided_restores SET status = ?, phase = ?, dump_file = ?, dump_size_bytes = ?,
                           safety_dump_path = ?, safety_dump_size_bytes = ?, database_changed = ?, db_owner = ?,
                           stopped_containers = ?, steps = ?, instructions = ?, warnings = ?,
                           bytes_done = ?, bytes_total = ?, error_message = ?,
                           completed_at = COALESCE(?, completed_at), duration_seconds = COALESCE(?, duration_seconds)
                       WHERE id = ?""",
                    (
                        op.status,
                        op.phase,
                        op.dump_file,
                        op.dump_size_bytes,
                        op.safety_dump_path,
                        op.safety_dump_size_bytes,
                        None if op.database_changed is None else int(op.database_changed),
                        op.db_owner,
                        json.dumps(op.stopped_containers),
                        json.dumps(op.steps),
                        json.dumps(op.instructions),
                        json.dumps(op.warnings),
                        op.bytes_done,
                        op.bytes_total,
                        op.error_message,
                        completed_at,
                        duration,
                        op.id,
                    ),
                )
                await db.commit()
        except Exception as exc:
            logger.error("Could not save guided restore %s: %s", op.id, exc)

    async def _finish(self, op: _Operation) -> None:
        if op.status == STATUS_RUNNING:
            op.status = STATUS_FAILED
            op.error_message = op.error_message or "The restore stopped unexpectedly"
        op.phase = None
        await self._save(op, final=True)
        what = "Undo" if op.kind == "undo" else "Database restore"
        if op.status == STATUS_COMPLETED:
            message, severity = f"{what} of {op.ref.label} completed", "info"
        elif op.status == STATUS_WARNINGS:
            message, severity = f"{what} of {op.ref.label} completed with warnings: {'; '.join(op.warnings)}", "warning"
        elif op.status == STATUS_ACTION_REQUIRED:
            message, severity = f"Restored {op.ref.label} to {op.source_path}; finish the swap by hand", "info"
        else:
            message, severity = f"{what} of {op.ref.label} failed: {op.error_message}", "error"
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await log_activity(
                    db,
                    "restore",
                    "database_restore_completed",
                    message[:500],
                    {
                        "guided_restore_id": op.id,
                        "kind": op.kind,
                        "status": op.status,
                        "container_name": op.ref.container_name,
                        "db_name": op.ref.db_name,
                        "snapshot_id": op.snapshot_id,
                        "database_changed": op.database_changed,
                    },
                    severity=severity,
                )
        except Exception as exc:
            logger.warning("Could not record guided restore activity: %s", exc)
        log = logger.info if op.status in (STATUS_COMPLETED, STATUS_ACTION_REQUIRED) else logger.warning
        log("Guided restore %s (%s %s): %s", op.id, op.kind, op.ref.label, op.status)

    async def _row(self, op_id: str) -> dict | None:
        if not re.fullmatch(r"[0-9a-f]{32}", op_id or ""):
            return None
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(self._SELECT + " WHERE g.id = ?", (op_id,))
                row = await cursor.fetchone()
        except aiosqlite.OperationalError:
            return None
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row) -> dict:
        data = dict(row)
        for column in _ROW_JSON_COLUMNS:
            try:
                data[column] = json.loads(data.get(column) or "[]")
            except (TypeError, ValueError):
                data[column] = []
        return data

    def _public(self, row: dict) -> dict:
        safety = self._owned_safety_dump(row) if row.get("method") == METHOD_LOAD else None
        can_undo = bool(safety is not None and row["status"] != STATUS_RUNNING and row.get("database_changed") != 0)
        running = row["status"] == STATUS_RUNNING
        return {
            "id": row["id"],
            "kind": row["kind"],
            "parent_id": row.get("parent_id"),
            "status": row["status"],
            "phase": row.get("phase"),
            "method": row["method"],
            "container_name": row["container_name"],
            "db_type": row["db_type"],
            "db_name": row["db_name"],
            "host_path": row.get("host_path"),
            "target_id": row.get("target_id"),
            "target_name": row.get("target_name"),
            "snapshot_id": row.get("snapshot_id"),
            "dump_file": row.get("dump_file"),
            "dump_size_bytes": row.get("dump_size_bytes") or 0,
            "safety_dump": (
                {
                    "file": os.path.basename(row["safety_dump_path"]),
                    "path": row["safety_dump_path"],
                    "size_bytes": row.get("safety_dump_size_bytes") or 0,
                    "available": safety is not None,
                }
                if row.get("safety_dump_path")
                else None
            ),
            "database_changed": None if row.get("database_changed") is None else bool(row["database_changed"]),
            "stop_containers": row.get("stop_containers") or [],
            "stopped_containers": row.get("stopped_containers") or [],
            "steps": row.get("steps") or [],
            "instructions": row.get("instructions") or [],
            "warnings": row.get("warnings") or [],
            "progress": (
                {"bytes_done": row.get("bytes_done") or 0, "bytes_total": row.get("bytes_total") or 0}
                if running and row.get("phase") == "load"
                else None
            ),
            "error_message": row.get("error_message") or None,
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "duration_seconds": row.get("duration_seconds"),
            "can_undo": can_undo,
        }
