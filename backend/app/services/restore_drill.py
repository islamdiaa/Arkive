"""Restore drills: prove database dumps restore FROM the backup repository.

Weekly verification only inspects the dump files sitting on local disk. A
drill goes further: it restores dumps out of a snapshot with restic and loads
them into a database instance, the way a real disaster recovery would.

- SQLite dumps are opened read-only and must pass ``PRAGMA integrity_check``.
- Postgres / MariaDB / MySQL dumps are loaded into a throwaway container
  started from the source container's own image (no network, CPU/memory
  capped, labelled ``arkive.drill``), then the restored user tables are
  counted and compared with the ``CREATE TABLE`` statements in the dump.

A drill never holds the backup lock (restic restore is non-exclusive) and
never touches a live database container. Its throwaway containers and scratch
files are always removed, and a crash mid-drill is cleaned up at the next
startup (``recover_after_restart`` / ``remove_leftover_containers``).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite

from app.core.activity import log_activity
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.backup_engine import ResticError
from app.services.lock_manager import BackupLockManager
from app.services.settings_store import get_int_setting
from app.utils.mounts import host_path_for, running_in_container
from app.utils.redact import redact_credentials

logger = logging.getLogger("arkive.restore_drill")

# Written into the dump directory by DBDumper (read here as plain JSON so this
# module doesn't depend on the dumper's import surface).
DUMP_MANIFEST_NAME = "arkive-dumps-manifest.json"
DRILL_LABEL = "arkive.drill"
DRILL_DIR_PREFIX = "drill-"
# Ownership marker written into every drill scratch dir; the startup sweep only
# deletes directories that carry it (users may restore into restore_dir too).
DRILL_MARKER = ".arkive-drill"
_DRILL_DIR_RE = re.compile(r"^drill-([0-9a-f]{32})$")

MIB = 1024 * 1024
# Without a host path for the restore dir, the drill database lives in
# Docker's own storage (docker.img on Unraid), so only small dumps are drilled.
NO_HOST_PATH_MAX_BYTES = 512 * MIB
MAX_MANIFEST_BYTES = 16 * MIB

SQL_SERVER_TYPES = frozenset({"postgres", "mariadb", "mysql"})
DRILLABLE_TYPES = SQL_SERVER_TYPES | {"sqlite"}
NOT_DRILLED_TYPES = frozenset({"mongodb", "redis"})

PG_DUMP_TRAILER = b"PostgreSQL database dump complete"
MYSQL_DUMP_TRAILER = b"-- Dump completed"

_SQLITE_UNVERIFIABLE_MARKERS = (
    "no such collation sequence",
    "no such module",
    "no such function",
    "no such tokenizer",
)
_SQLITE_EXTENSIONS = (".sqlite3", ".sqlite", ".db")
# Dump *file* names are sanitised by the dumper (safe_name); restic --include
# treats them as patterns, so anything else is refused.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_UNSAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9_.\-]")
_DUMP_NAME_RE = re.compile(r"^(?P<prefix>.+)_(?P<ts>\d{8}_\d{6})\.(?P<ext>[A-Za-z0-9.]+)$")
_NON_DUMP_FILES = {DUMP_MANIFEST_NAME, "arkive_self.db"}
_NON_DUMP_PREFIXES = ("flash_",)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# Paths inside the throwaway container (chosen here, never user-controlled).
CONTAINER_DUMP_DIR = "/arkive-drill"
CONTAINER_DUMP_FILE = "/arkive-drill/dump.sql"
CONTAINER_LOAD_ERR = "/tmp/arkive-drill-load.err"  # nosec B108 - inside the throwaway drill container

# Fixed shell scripts: variable parts arrive through the exec environment and
# are only ever expanded inside double quotes.
_PG_LOAD_SCRIPT = (
    'psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$ARKIVE_DRILL_USER" '
    '-d "$ARKIVE_DRILL_MAINTDB" -f ' + CONTAINER_DUMP_FILE + " >/dev/null 2>" + CONTAINER_LOAD_ERR
)
_MYSQL_LOAD_SCRIPT = (
    '"$ARKIVE_DRILL_CLIENT" -h 127.0.0.1 --protocol=tcp -uroot <'
    + CONTAINER_DUMP_FILE
    + " >/dev/null 2>"
    + CONTAINER_LOAD_ERR
)

_PG_COUNT_SQL = (
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
    "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass "
    "AND d.objid = c.oid AND d.deptype = 'e')"
)
_MYSQL_COUNT_SQL = (
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
    "AND table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')"
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(value: str) -> str:
    """Same file-name sanitisation DBDumper uses for dump names."""
    return _UNSAFE_CHARS_RE.sub("_", value)


def valid_pg_dbname(name: str | None) -> bool:
    """Same rule as the dumper: names only ever reach psql as one argv element
    or an env var, so the hazards are libpq reading them as a connection string
    ("=", a URI with "/") or as an option (leading "-"), plus control characters.
    Spaces, capitals and Unicode are fine."""
    if not name or name.startswith("-") or "=" in name or "/" in name:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in name)


def _psql_token(text: str) -> str | None:
    """First psql meta-command argument: a double-quoted token ("" is a literal
    quote) or a bare word."""
    text = text.strip()
    if not text:
        return None
    if not text.startswith('"'):
        return text.split()[0]
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            if text[i + 1 : i + 2] == '"':
                out.append('"')
                i += 2
                continue
            return "".join(out)
        out.append(ch)
        i += 1
    return None  # unterminated quote


def _conninfo_value(conninfo: str, key: str) -> str | None:
    """Value of ``key`` in a libpq "k=v k='quoted v'" string (backslash escapes;
    doubled single quotes tolerated)."""
    i, n = 0, len(conninfo)
    while i < n:
        while i < n and conninfo[i].isspace():
            i += 1
        start = i
        while i < n and conninfo[i] != "=" and not conninfo[i].isspace():
            i += 1
        name = conninfo[start:i]
        while i < n and conninfo[i].isspace():
            i += 1
        if i >= n or conninfo[i] != "=":
            return None
        i += 1
        while i < n and conninfo[i].isspace():
            i += 1
        value: list[str] = []
        if i < n and conninfo[i] == "'":
            i += 1
            while i < n:
                ch = conninfo[i]
                if ch == "\\" and i + 1 < n:
                    value.append(conninfo[i + 1])
                    i += 2
                elif ch == "'" and conninfo[i + 1 : i + 2] == "'":
                    value.append("'")
                    i += 2
                elif ch == "'":
                    i += 1
                    break
                else:
                    value.append(ch)
                    i += 1
            else:
                return None  # unterminated quote
        else:
            while i < n and not conninfo[i].isspace():
                if conninfo[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(conninfo[i])
                i += 1
        if name == key:
            return "".join(value)
    return None


def parse_pg_connect(arg: str) -> str | None:
    """Database name from the argument of a pg_dump ``\\connect`` line.

    pg_dump writes ``\\connect appdb``, ``\\connect "MixedCase"`` or, for any
    other name, ``\\connect -reuse-previous=on "dbname='my db'"``.
    """
    arg = arg.strip()
    reuse_previous = arg.startswith("-reuse-previous=")
    if reuse_previous:
        arg = arg.partition(" ")[2]
    token = _psql_token(arg)
    if token is None:
        return None
    if reuse_previous or (token.startswith("dbname=") and not arg.startswith('"')):
        return _conninfo_value(token, "dbname")
    return token


def _mb(size_bytes: int) -> int:
    return max(0, round(size_bytes / MIB))


def _human_size(size_bytes: int) -> str:
    if size_bytes >= MIB:
        return f"{size_bytes / MIB:.1f} MB"
    return f"{max(1, round(size_bytes / 1024))} KB"


class DrillAlreadyRunningError(RuntimeError):
    """A drill is already in progress."""


class DrillSkip(Exception):  # noqa: N818 - control flow, not an error
    """A database could not be tested for an environmental reason (not a dump failure)."""


class DrillFailure(Exception):  # noqa: N818 - a test result, not an error
    """The dump itself is broken (corrupt, truncated, does not load)."""


class DrillRepositoryError(Exception):
    """The backup repository could not be read (listing or restore failed)."""


# ---------------------------------------------------------------------------
# Candidates and selection
# ---------------------------------------------------------------------------


@dataclass
class DumpCandidate:
    file: str  # basename inside the dump directory
    container_name: str
    db_type: str
    db_name: str = ""
    size_bytes: int = 0
    image: str | None = None
    kind: str = "database"  # database | globals
    in_snapshot: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.container_name, self.db_name or "")

    @property
    def is_sql_server(self) -> bool:
        return self.db_type in SQL_SERVER_TYPES


@dataclass
class DrillResult:
    container_name: str
    db_type: str
    db_name: str
    dump_file: str
    dump_size_bytes: int
    status: str  # passed | failed | skipped
    detail: str
    duration_seconds: float = 0.0

    @classmethod
    def for_candidate(cls, cand: DumpCandidate, status: str, detail: str, duration: float = 0.0) -> DrillResult:
        return cls(
            container_name=cand.container_name,
            db_type=cand.db_type,
            db_name=cand.db_name,
            dump_file=cand.file,
            dump_size_bytes=cand.size_bytes,
            status=status,
            detail=redact_credentials(detail)[:2000],
            duration_seconds=round(duration, 2),
        )


@dataclass
class DrillOutcome:
    status: str = "skipped"
    error_message: str | None = None
    target_id: str | None = None
    target_name: str | None = None
    snapshot_id: str | None = None
    results: list[DrillResult] = field(default_factory=list)

    def skip(self, reason: str) -> DrillOutcome:
        self.status = "skipped"
        self.error_message = reason
        return self

    @property
    def tested(self) -> list[DrillResult]:
        return [r for r in self.results if r.status in ("passed", "failed")]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")


def candidates_from_manifest(manifest: dict, files: dict[str, dict]) -> list[DumpCandidate]:
    """Build candidates from ``arkive-dumps-manifest.json`` entries."""
    candidates: list[DumpCandidate] = []
    for entry in manifest.get("dumps") or []:
        if not isinstance(entry, dict):
            continue
        name = os.path.basename(str(entry.get("file") or ""))
        if not name or not _SAFE_NAME_RE.match(name):
            continue
        listing = files.get(name)
        size = listing.get("size") if listing else None
        if not isinstance(size, int):
            size = entry.get("size_bytes") if isinstance(entry.get("size_bytes"), int) else 0
        candidates.append(
            DumpCandidate(
                file=name,
                container_name=str(entry.get("container_name") or ""),
                db_type=str(entry.get("db_type") or "").lower(),
                db_name=str(entry.get("db_name") or ""),
                size_bytes=size,
                image=str(entry.get("image")) if entry.get("image") else None,
                kind=str(entry.get("kind") or "database").lower(),
                in_snapshot=listing is not None,
            )
        )
    return candidates


def infer_candidates(
    files: dict[str, dict], known: list[dict], images: dict[str, str] | None = None
) -> list[DumpCandidate]:
    """Infer candidates for snapshots written before the dump manifest existed.

    Dump files are named ``<safe container>_<safe db label>_<YYYYmmdd_HHMMSS>.<ext>``;
    ``known`` holds ``{"container_name", "db_type", "db_name"}`` records from
    job_run_databases / discovered_containers.
    """
    images = images or {}
    patterns: list[tuple[str, dict]] = []
    containers: dict[str, str] = {}
    for record in known:
        container = str(record.get("container_name") or "")
        db_name = str(record.get("db_name") or "")
        db_type = str(record.get("db_type") or "").lower()
        if not container or not db_type:
            continue
        containers[_safe(container)] = container
        if db_name:
            patterns.append((f"{_safe(container)}_{_safe(db_name)}", record))
    # Longest pattern first so "app_db_x" beats "app_db".
    patterns.sort(key=lambda p: len(p[0]), reverse=True)

    newest: dict[tuple[str, str, str, str], tuple[str, DumpCandidate]] = {}
    for name, listing in files.items():
        if (
            name in _NON_DUMP_FILES
            or name.startswith(_NON_DUMP_PREFIXES)
            or name.startswith(".")
            or name.endswith(_SIDECAR_SUFFIXES)
            or ".partial" in name
            or not _SAFE_NAME_RE.match(name)
        ):
            continue  # not a finished dump (temp file, sidecar, flash archive, ...)
        match = _DUMP_NAME_RE.match(name)
        if not match:
            continue
        prefix, ts, ext = match.group("prefix"), match.group("ts"), match.group("ext").lower()
        size = listing.get("size") if isinstance(listing.get("size"), int) else 0
        cand: DumpCandidate | None = None
        for pattern, record in patterns:
            if prefix == pattern or (
                prefix.startswith(pattern + "-") and re.fullmatch(r"[0-9a-f]{8}", prefix[len(pattern) + 1 :])
            ):
                container = str(record["container_name"])
                cand = DumpCandidate(
                    file=name,
                    container_name=container,
                    db_type=str(record["db_type"]).lower(),
                    db_name=str(record["db_name"]),
                    size_bytes=size,
                    image=images.get(container),
                )
                break
        # New-style names end in an identity hash: "<container>_globals-<hash8>".
        roles_prefix = re.sub(r"-[0-9a-f]{8}$", "", prefix)
        if cand is None and roles_prefix.endswith("_globals") and roles_prefix[: -len("_globals")] in containers:
            container = containers[roles_prefix[: -len("_globals")]]
            cand = DumpCandidate(
                file=name,
                container_name=container,
                db_type="postgres",
                db_name="globals",
                size_bytes=size,
                image=images.get(container),
                kind="globals",
            )
        if cand is None:
            if ext == "rdb":
                db_type = "redis"
            elif ext.startswith("archive"):
                db_type = "mongodb"
            elif any(name.endswith(e) for e in _SQLITE_EXTENSIONS):
                db_type = "sqlite"
            else:
                db_type = "unknown"
            cand = DumpCandidate(
                file=name,
                container_name=prefix,
                db_type=db_type,
                db_name=name if db_type == "sqlite" else "",
                size_bytes=size,
            )
        dedupe = (cand.container_name, cand.db_type, cand.db_name, cand.kind)
        if dedupe not in newest or ts > newest[dedupe][0]:
            newest[dedupe] = (ts, cand)
    return [cand for _, cand in sorted(newest.values(), key=lambda item: item[1].file)]


def select_candidates(
    candidates: list[DumpCandidate],
    history: dict[tuple[str, str], tuple[str, str]],
    max_databases: int,
    max_bytes: int,
) -> tuple[list[DumpCandidate], list[tuple[DumpCandidate, str, str]]]:
    """Choose which dumps to drill.

    ``history`` maps (container, db_name) -> (last drilled at, last status).
    Databases whose last drill failed go first (so a failure is re-checked
    rather than rotated away), then never-drilled ones, then the least
    recently drilled. At least one SQL-server database is included when any
    exists, and with two or more slots at least one SQLite database too.

    Returns (chosen, [(candidate, status, reason)]) for everything not chosen
    for a reason: a manifest entry missing from the snapshot is ``failed`` (a
    dump that should be restorable is not there); intentional exclusions
    (roles dump, untested type, over the size cap) are ``skipped``.
    """
    excluded: list[tuple[DumpCandidate, str, str]] = []
    testable: list[DumpCandidate] = []
    limit_mb = max(1, max_bytes // MIB)
    for cand in candidates:
        if not cand.in_snapshot:
            excluded.append((cand, "failed", "listed in the snapshot's dump manifest but missing from the snapshot"))
        elif cand.kind == "globals":
            excluded.append((cand, "skipped", "roles/globals dump; not drill-tested"))
        elif cand.db_type in NOT_DRILLED_TYPES:
            excluded.append((cand, "skipped", f"{cand.db_type} dumps are not drill-tested"))
        elif cand.db_type not in DRILLABLE_TYPES:
            excluded.append(
                (cand, "skipped", "database type unknown (no manifest entry or discovery record); not drill-tested")
            )
        elif cand.size_bytes > max_bytes:
            excluded.append((cand, "skipped", f"dump is {_mb(cand.size_bytes)} MB; limit is {limit_mb} MB"))
        else:
            testable.append(cand)

    def order(cand: DumpCandidate):
        last_at, last_status = history.get(cand.key, ("", ""))
        return (0 if last_status == "failed" else 1, last_at, cand.container_name, cand.db_name, cand.file)

    testable.sort(key=order)
    chosen = testable[: max(1, max_databases)]

    def swap_in(predicate, protect) -> None:
        if not chosen or any(predicate(c) for c in chosen):
            return
        extra = next((c for c in testable if predicate(c) and c not in chosen), None)
        if extra is None:
            return
        for i in range(len(chosen) - 1, -1, -1):
            if not protect(i):
                chosen[i] = extra
                return

    swap_in(lambda c: c.is_sql_server, lambda i: False)
    if len(chosen) >= 2:
        sql_count = lambda: sum(1 for c in chosen if c.is_sql_server)  # noqa: E731
        swap_in(lambda c: c.db_type == "sqlite", lambda i: chosen[i].is_sql_server and sql_count() == 1)
    chosen.sort(key=order)
    return chosen, excluded


# ---------------------------------------------------------------------------
# Dump file checks (blocking)
# ---------------------------------------------------------------------------


def verify_sqlite_file(path: Path) -> tuple[str, str]:
    """Open a restored SQLite file read-only and check it. Returns (status, detail)."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "failed", f"restored file is not readable: {exc}"
    if size == 0:
        return "failed", "restored file is empty"
    uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        return "failed", f"cannot open restored SQLite file: {exc}"
    try:
        try:
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        except sqlite3.DatabaseError as exc:
            return "failed", f"not a valid SQLite database: {exc}"
        unverifiable = None
        try:
            rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.Error as exc:
            message = str(exc)
            if not any(m in message.lower() for m in _SQLITE_UNVERIFIABLE_MARKERS):
                return "failed", f"integrity_check error: {message}"
            unverifiable, rows = message, []
        if rows and rows != ["ok"]:
            text = "; ".join(rows[:5])
            if not any(m in text.lower() for m in _SQLITE_UNVERIFIABLE_MARKERS):
                return "failed", f"integrity_check reported: {text[:500]}"
            unverifiable = text
        if tables == 0:
            return "failed", "no tables found in a non-empty database file"
        if unverifiable:
            return "passed", (
                f"{tables} tables; integrity not verifiable with stock SQLite "
                f"(app-specific collation/extension: {unverifiable[:200]})"
            )
        return "passed", f"integrity_check ok; {tables} tables"
    finally:
        conn.close()


@dataclass
class PreparedDump:
    path: Path
    size_bytes: int
    create_tables: int
    database: str | None
    trailer_ok: bool


def prepare_sql_dump(src: Path, dest: Path, db_type: str, min_free_bytes: int = 0) -> PreparedDump:
    """Put a plain-SQL copy of the dump at ``dest`` (gunzipping if needed) and
    scan it in one streaming pass: CREATE TABLE count, target database (pg
    ``\\connect``), completion trailer. Never reads the whole file into memory.

    While gunzipping, stops with DrillSkip if free space on ``dest``'s
    filesystem drops below ``min_free_bytes`` (the restore dir usually shares
    a disk with live appdata).
    """
    with open(src, "rb") as fh:
        gzipped = fh.read(2) == b"\x1f\x8b"
    postgres = db_type == "postgres"
    create_tables = 0
    database: str | None = None
    in_copy = False
    at_line_start = True

    if gzipped:
        reader = gzip.open(src, "rb")
        writer = open(dest, "wb")
    else:
        if src != dest:
            os.replace(src, dest)
        reader = open(dest, "rb")
        writer = None
    written = 0
    next_space_check = 0
    try:
        while True:
            line = reader.readline(MIB)
            if not line:
                break
            if writer is not None:
                writer.write(line)
                written += len(line)
                if min_free_bytes and written >= next_space_check:
                    next_space_check = written + 64 * MIB
                    if shutil.disk_usage(dest.parent).free < min_free_bytes:
                        raise DrillSkip("not enough free disk space to decompress the dump")
            starts_line = at_line_start
            at_line_start = line.endswith(b"\n")
            if not starts_line:
                continue  # continuation of a line longer than MIB
            if in_copy:
                if line.rstrip(b"\r\n") == b"\\.":
                    in_copy = False
                continue
            if line.startswith(b"CREATE TABLE ") or line.startswith(b"CREATE UNLOGGED TABLE "):
                create_tables += 1
            elif postgres and line.startswith(b"COPY ") and line.rstrip().endswith(b"FROM stdin;"):
                in_copy = True
            elif postgres and database is None and line.startswith(b"\\connect "):
                database = parse_pg_connect(
                    line[len(b"\\connect ") :].rstrip(b"\r\n").decode("utf-8", errors="replace")
                )
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise DrillFailure(f"corrupt gzip stream: {exc}") from exc
    finally:
        reader.close()
        if writer is not None:
            writer.close()
    if gzipped:
        try:
            os.remove(src)
        except OSError:
            pass

    size = dest.stat().st_size
    with open(dest, "rb") as fh:
        fh.seek(max(0, size - 4096))
        tail = fh.read()
    trailer = PG_DUMP_TRAILER if postgres else MYSQL_DUMP_TRAILER
    return PreparedDump(
        path=dest, size_bytes=size, create_tables=create_tables, database=database, trailer_ok=trailer in tail
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class _ServerSpec:
    db_type: str
    image: str
    data_dir: str
    env: dict[str, str]
    password: str
    pg_user: str = "postgres"
    client: str | None = None  # mysql/mariadb client binary
    admin: str | None = None  # mysqladmin/mariadb-admin

    @property
    def is_postgres(self) -> bool:
        return self.db_type == "postgres"


class RestoreDrillEngine:
    """Runs restore drills and records them in restore_drills / restore_drill_results."""

    idle_wait_seconds: float = 3 * 3600
    idle_poll_seconds: float = 15
    ready_timeout_seconds: float = 180
    poll_seconds: float = 2
    min_load_timeout_seconds: float = 300
    max_load_timeout_seconds: float = 1800
    container_mem_limit: str = "2g"
    container_nano_cpus: int = 2_000_000_000
    container_shm_size: str = "256m"
    # Head-room always left free on the restore dir's disk.
    min_free_bytes: int = 1024 * MIB

    def __init__(
        self,
        config: ArkiveConfig,
        backup_engine=None,
        docker_client=None,
        notifier=None,
        event_bus=None,
        lock_manager: BackupLockManager | None = None,
    ):
        self.config = config
        self.backup_engine = backup_engine
        self.docker_client = docker_client
        self.notifier = notifier
        self.event_bus = event_bus
        self.lock_manager = lock_manager or BackupLockManager(config.config_dir)
        self._current_drill_id: str | None = None
        self._task: asyncio.Task | None = None

    # ---- public API ----

    @property
    def running_drill_id(self) -> str | None:
        return self._current_drill_id

    def is_running(self) -> bool:
        return self._current_drill_id is not None

    async def start_drill(self, trigger: str = "manual") -> str:
        """Create the drill row and run it in the background. Returns the drill id."""
        drill_id = await self._claim(trigger)
        self._task = asyncio.create_task(self._execute(drill_id, trigger))
        return drill_id

    async def run_drill(self, trigger: str = "scheduled") -> dict:
        """Run a drill to completion (scheduler path)."""
        try:
            drill_id = await self._claim(trigger)
        except DrillAlreadyRunningError:
            logger.info("Restore drill already running; %s run skipped", trigger)
            return {"status": "skipped", "reason": "already_running"}
        return await self._execute(drill_id, trigger)

    async def recover_after_restart(self) -> None:
        """Startup: fail drills left 'running' by a crash and delete their scratch dirs."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute(
                    "UPDATE restore_drills SET status = 'failed', completed_at = ?, "
                    "error_message = 'Interrupted by restart' WHERE status = 'running'",
                    (_now(),),
                )
                await db.commit()
                if cursor.rowcount:
                    logger.warning("Marked %d interrupted restore drill(s) as failed", cursor.rowcount)
        except Exception as exc:
            logger.warning("Could not mark interrupted restore drills: %s", exc)
        try:
            await asyncio.to_thread(self._remove_stale_dirs)
        except Exception as exc:
            logger.warning("Could not remove stale drill directories: %s", exc)

    async def remove_leftover_containers(self) -> int:
        """Startup: remove throwaway containers left behind by a crash mid-drill."""
        if self.docker_client is None:
            return 0
        try:
            return await asyncio.to_thread(self._remove_leftover_containers_blocking)
        except Exception as exc:
            logger.warning("Could not sweep leftover drill containers: %s", exc)
            return 0

    # ---- orchestration ----

    async def _claim(self, trigger: str) -> str:
        if self._current_drill_id is not None:
            raise DrillAlreadyRunningError("A restore drill is already running")
        drill_id = uuid.uuid4().hex
        self._current_drill_id = drill_id  # set before the first await: no double start
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    "INSERT INTO restore_drills (id, trigger, status, started_at) VALUES (?, ?, 'running', ?)",
                    (drill_id, trigger, _now()),
                )
                await db.commit()
        except Exception:
            self._current_drill_id = None
            raise
        return drill_id

    async def _execute(self, drill_id: str, trigger: str) -> dict:
        started = time.monotonic()
        drill_dir = Path(self.config.restore_dir) / f"{DRILL_DIR_PREFIX}{drill_id}"
        outcome = DrillOutcome()
        created_dir = False
        try:
            await self._publish("drill:started", {"drill_id": drill_id})
            logger.info("Restore drill %s started (%s)", drill_id, trigger)
            await asyncio.to_thread(self._create_drill_dir, drill_dir, drill_id)
            created_dir = True
            outcome = await self._perform(drill_id, drill_dir)
        except Exception as exc:
            logger.exception("Restore drill %s aborted", drill_id)
            outcome.status = "failed"
            outcome.error_message = redact_credentials(f"Drill aborted: {exc}")[:1000]
        finally:
            if created_dir:
                try:
                    await asyncio.to_thread(shutil.rmtree, drill_dir, True)
                except Exception:
                    pass
            if self._current_drill_id == drill_id:
                self._current_drill_id = None

        duration = int(round(time.monotonic() - started))
        summary = {
            "drill_id": drill_id,
            "status": outcome.status,
            "databases_tested": len(outcome.tested),
            "databases_passed": outcome.passed,
            "databases_failed": outcome.failed,
        }
        await self._persist(drill_id, outcome, duration)
        await self._record_activity(drill_id, outcome, duration)
        await self._publish("drill:completed", summary)
        if outcome.status == "failed":
            await self._notify_failure(outcome)
        logger.info(
            "Restore drill %s finished: %s (%d passed, %d failed, %ds)",
            drill_id,
            outcome.status,
            outcome.passed,
            outcome.failed,
            duration,
        )
        return {**summary, "error_message": outcome.error_message}

    async def _perform(self, drill_id: str, drill_dir: Path) -> DrillOutcome:
        outcome = DrillOutcome()
        async with aiosqlite.connect(self.config.db_path) as db:
            max_databases = await get_int_setting(db, "restore_drill_max_databases", minimum=1)
            max_dump_mb = await get_int_setting(db, "restore_drill_max_dump_mb", minimum=1)

        if self.backup_engine is None:
            return outcome.skip("backup engine not available")
        if not await self._wait_until_idle():
            return outcome.skip("a backup or restore was still running; drill postponed")

        targets = await self._load_targets()
        if not targets:
            return outcome.skip("no enabled storage targets")
        try:
            found = await self._find_dump_snapshot(targets)
        except DrillRepositoryError as exc:
            return self._repository_failure(outcome, exc)
        if found is None:
            return outcome.skip("no snapshot contains database dumps")
        target, snapshot_id, short_id, entries = found
        outcome.target_id = target.get("id")
        outcome.target_name = target.get("name") or target.get("id")
        outcome.snapshot_id = short_id

        files = {e["name"]: e for e in entries if e.get("type") == "file" and e.get("name")}
        candidates: list[DumpCandidate] = []
        manifest = None
        if DUMP_MANIFEST_NAME in files:
            try:
                manifest = await self._read_manifest(target, snapshot_id, short_id, drill_dir)
            except DrillRepositoryError as exc:
                return self._repository_failure(outcome, exc)
        if manifest is not None:
            candidates = candidates_from_manifest(manifest, files)
        else:
            known, images = await self._known_databases()
            candidates = infer_candidates(files, known, images)
        if not candidates:
            return outcome.skip(f"snapshot {short_id} contains no database dumps")

        history = await self._drill_history()
        chosen, excluded = select_candidates(candidates, history, max_databases, max_dump_mb * MIB)
        outcome.results.extend(DrillResult.for_candidate(c, status, reason) for c, status, reason in excluded)

        host_restore_dir = await self._resolve_host_restore_dir() if chosen else None
        for index, cand in enumerate(chosen):
            result = await self._drill_one(drill_id, index, cand, target, snapshot_id, drill_dir, host_restore_dir)
            outcome.results.append(result)

        if outcome.failed:
            outcome.status = "failed"
            names = ", ".join(
                f"{r.container_name}/{r.db_name or r.dump_file}" for r in outcome.results if r.status == "failed"
            )
            outcome.error_message = f"{outcome.failed} database(s) did not restore: {names}"[:1000]
        elif outcome.passed:
            outcome.status = "passed"
        elif not chosen:
            outcome.skip(f"snapshot {short_id} has no drill-testable database dumps ({len(excluded)} skipped)")
        else:
            chosen_files = {c.file for c in chosen}
            reasons = [
                f"{r.container_name}/{r.db_name or r.dump_file}: {r.detail}"
                for r in outcome.results
                if r.status == "skipped" and r.dump_file in chosen_files
            ]
            outcome.skip(
                (
                    f"no database could be tested ({len(reasons)} selected, none could be "
                    f"started or restored for testing): " + "; ".join(reasons)
                )[:1000]
            )
        return outcome

    @staticmethod
    def _repository_failure(outcome: DrillOutcome, exc: Exception) -> DrillOutcome:
        target = getattr(exc, "target", None)
        if target:
            outcome.target_id = target.get("id")
            outcome.target_name = target.get("name") or target.get("id")
        outcome.snapshot_id = getattr(exc, "snapshot_id", None) or outcome.snapshot_id
        outcome.status = "failed"
        outcome.error_message = redact_credentials(str(exc))[:1000]
        return outcome

    @staticmethod
    def _create_drill_dir(drill_dir: Path, drill_id: str) -> None:
        drill_dir.parent.mkdir(parents=True, exist_ok=True)
        drill_dir.mkdir()  # never adopt an existing directory
        (drill_dir / DRILL_MARKER).write_text(drill_id)

    async def _drill_one(
        self,
        drill_id: str,
        index: int,
        cand: DumpCandidate,
        target: dict,
        snapshot_id: str,
        drill_dir: Path,
        host_restore_dir: str | None,
    ) -> DrillResult:
        started = time.monotonic()
        work = drill_dir / f"db-{index}"
        try:
            await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
            free = await asyncio.to_thread(lambda: shutil.disk_usage(work).free)
            needed = cand.size_bytes + self.min_free_bytes
            if free < needed:
                raise DrillSkip(
                    f"not enough free space in {self.config.restore_dir} "
                    f"({_mb(free)} MB free, ~{_mb(needed)} MB needed)"
                )
            dump_dir = str(self.config.dump_dir).rstrip("/")
            restore_to = work / "restore"
            result = await self.backup_engine.restore(
                target=target,
                snapshot_id=snapshot_id,
                paths=[f"{dump_dir}/{cand.file}"],
                restore_to=str(restore_to),
            )
            if result.get("status") != "success":
                raise DrillFailure(
                    f"restore from the repository failed: {result.get('error') or 'restic returned an error'}"
                )
            restored = restore_to / dump_dir.lstrip("/") / cand.file
            if not await asyncio.to_thread(restored.is_file):
                raise DrillFailure("dump file missing after restore")
            if cand.db_type == "sqlite":
                status, detail = await asyncio.to_thread(verify_sqlite_file, restored)
            else:
                status, detail = await asyncio.to_thread(
                    self._drill_sql_blocking,
                    drill_id,
                    index,
                    cand,
                    restored,
                    work,
                    host_restore_dir,
                )
        except DrillSkip as exc:
            status, detail = "skipped", str(exc)
        except DrillFailure as exc:
            status, detail = "failed", str(exc)
        except Exception as exc:
            if type(exc).__name__ == "DockerUnavailableError":
                status, detail = "skipped", f"Docker is not reachable: {exc}"
            else:
                # An unexpected error means the dump was not (fully) tested; it
                # is not evidence that the dump is broken.
                logger.exception("Drill of %s/%s errored", cand.container_name, cand.db_name)
                status, detail = "skipped", f"drill error, dump not tested: {exc}"
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)
        log = logger.warning if status == "failed" else logger.info
        log("Drill %s/%s (%s): %s — %s", cand.container_name, cand.db_name or cand.file, cand.db_type, status, detail)
        return DrillResult.for_candidate(cand, status, detail, time.monotonic() - started)

    # ---- SQL server drill (blocking; runs in a worker thread) ----

    def _docker(self):
        if self.docker_client is None:
            raise DrillSkip("Docker is not available")
        return self.docker_client

    def _drill_sql_blocking(
        self, drill_id: str, index: int, cand: DumpCandidate, restored: Path, work: Path, host_restore_dir: str | None
    ) -> tuple[str, str]:
        dump_dir = work / "dump"
        data_dir = work / "data"
        dump_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        prepared = prepare_sql_dump(restored, dump_dir / "dump.sql", cand.db_type, min_free_bytes=self.min_free_bytes)
        if not prepared.trailer_ok:
            raise DrillFailure("dump is truncated (no completion trailer)")

        # The loaded database typically takes 1-2x the plain dump size.
        needed = prepared.size_bytes * 2 + self.min_free_bytes
        free = shutil.disk_usage(work).free
        if free < needed:
            raise DrillSkip(
                f"not enough free space in {self.config.restore_dir} to load the dump "
                f"({_mb(free)} MB free, ~{_mb(needed)} MB needed)"
            )

        host_work = None
        if host_restore_dir:
            host_work = os.path.join(host_restore_dir, os.path.relpath(work, self.config.restore_dir))
        elif prepared.size_bytes > NO_HOST_PATH_MAX_BYTES:
            raise DrillSkip(
                f"dump is {_mb(prepared.size_bytes)} MB and the host path of "
                f"{self.config.restore_dir} could not be resolved; without it only dumps under "
                f"{NO_HOST_PATH_MAX_BYTES // MIB} MB are drilled (they would load into Docker's own storage)"
            )

        docker = self._docker()
        image, source_env, src_entrypoint, src_cmd = self._source_details(docker, cand)
        if not image:
            raise DrillSkip(f"source image for {cand.container_name} is unknown (container gone and no image recorded)")
        image_config = self._ensure_image(docker, image)
        spec = self._server_spec(cand.db_type, image, source_env, image_config)

        volumes = None
        if host_work:
            volumes = {
                os.path.join(host_work, "data"): {"bind": spec.data_dir, "mode": "rw"},
                os.path.join(host_work, "dump"): {"bind": CONTAINER_DUMP_DIR, "mode": "ro"},
            }

        attempts: list[tuple[Any, Any, str]] = []
        if (src_entrypoint or src_cmd) and (
            src_entrypoint != image_config.get("Entrypoint") or src_cmd != image_config.get("Cmd")
        ):
            attempts.append((src_entrypoint, src_cmd, "source container's command"))
        attempts.append((None, None, "image default command"))

        tar_path = None if host_work else self._build_dump_tar(prepared.path, work)
        notes: list[str] = []
        for attempt, (entrypoint, command, label) in enumerate(attempts):
            container = None
            try:
                # Until the server is ready nothing about the dump has been
                # tested: start-up problems (s6 images, required env, resource
                # limits, slow storage) are "skipped", never "failed".
                try:
                    container = self._create_container(
                        docker,
                        drill_id,
                        index,
                        attempt,
                        spec,
                        entrypoint,
                        command,
                        volumes,
                    )
                    if tar_path is not None:
                        with open(tar_path, "rb") as fh:
                            if not container.put_archive("/", fh):
                                raise DrillSkip("could not copy the dump into the drill container")
                    container.start()
                    ready, why = self._wait_ready(container, spec)
                except DrillSkip:
                    raise
                except Exception as exc:
                    if type(exc).__name__ == "DockerUnavailableError":
                        raise
                    raise DrillSkip(
                        f"could not start a throwaway {spec.db_type} server from {image}: "
                        f"{exc}; the dump was not tested"
                    ) from exc
                if not ready:
                    if why.startswith("container exited") and attempt + 1 < len(attempts):
                        notes.append(f"with the {label}: {why}")
                        continue
                    raise DrillSkip(f"database server from {image} did not start ({why}); the dump was not tested")
                # Server is up: from here on a problem is a real restore failure,
                # including unexpected errors (e.g. the server dying mid-load).
                try:
                    status, detail = self._load_and_check(container, spec, prepared, cand)
                except (DrillFailure, DrillSkip):
                    raise
                except Exception as exc:
                    if type(exc).__name__ == "DockerUnavailableError":
                        raise
                    raise DrillFailure(f"error while loading or verifying the dump: {exc}") from exc
                if notes:
                    detail += f" (retried with the {label}; {notes[0][:200]})"
                return status, detail
            finally:
                if container is not None:
                    self._remove_container(container)
                if attempt + 1 < len(attempts):
                    shutil.rmtree(data_dir, ignore_errors=True)
                    data_dir.mkdir(parents=True, exist_ok=True)
        raise DrillSkip("database server did not start")  # pragma: no cover - loop always returns

    def _source_details(self, docker, cand: DumpCandidate) -> tuple[str | None, dict, Any, Any]:
        attrs: dict = {}
        try:
            attrs = docker.containers.get(cand.container_name).attrs or {}
        except Exception as exc:
            if type(exc).__name__ == "DockerUnavailableError":
                raise
            logger.debug("Source container %s not inspectable: %s", cand.container_name, exc)
        config = attrs.get("Config") or {}
        env: dict[str, str] = {}
        for item in config.get("Env") or []:
            key, sep, value = str(item).partition("=")
            if sep:
                env[key] = value
        image = cand.image or config.get("Image")
        return image, env, config.get("Entrypoint"), config.get("Cmd")

    @staticmethod
    def _ensure_image(docker, image: str) -> dict:
        try:
            image_obj = docker.images.get(image)
        except Exception as exc:
            if type(exc).__name__ == "DockerUnavailableError":
                raise
            try:
                image_obj = docker.images.pull(image)
            except Exception as pull_exc:
                raise DrillSkip(f"image {image} is not available: {pull_exc}") from pull_exc
        return dict((getattr(image_obj, "attrs", None) or {}).get("Config") or {})

    @staticmethod
    def _server_spec(db_type: str, image: str, source_env: dict, image_config: dict) -> _ServerSpec:
        image_env: dict[str, str] = {}
        for item in image_config.get("Env") or []:
            key, sep, value = str(item).partition("=")
            if sep:
                image_env[key] = value
        password = secrets.token_urlsafe(24)
        if db_type == "postgres":
            env = {
                k: source_env[k]
                for k in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_INITDB_ARGS", "PGDATA")
                if source_env.get(k)
            }
            env["POSTGRES_PASSWORD"] = password
            data_dir = env.get("PGDATA") or image_env.get("PGDATA") or "/var/lib/postgresql/data"
            return _ServerSpec(
                db_type=db_type,
                image=image,
                data_dir=data_dir,
                env=env,
                password=password,
                pg_user=env.get("POSTGRES_USER") or "postgres",
            )
        volumes = image_config.get("Volumes") or {}
        data_dir = "/var/lib/mysql"
        if "/var/lib/mysql" not in volumes and "/config" in volumes:
            data_dir = "/config"  # linuxserver-style images
        env = {"MARIADB_ROOT_PASSWORD": password, "MYSQL_ROOT_PASSWORD": password}
        return _ServerSpec(db_type=db_type, image=image, data_dir=data_dir, env=env, password=password)

    def _create_container(
        self, docker, drill_id: str, index: int, attempt: int, spec: _ServerSpec, entrypoint, command, volumes
    ):
        kwargs: dict[str, Any] = {
            "image": spec.image,
            "environment": spec.env,
            "labels": {DRILL_LABEL: drill_id},
            "network_mode": "none",
            "nano_cpus": self.container_nano_cpus,
            "mem_limit": self.container_mem_limit,
            "shm_size": self.container_shm_size,
            "name": f"arkive-drill-{drill_id[:12]}-{index}-{attempt}",
        }
        if entrypoint is not None:
            kwargs["entrypoint"] = entrypoint
        if command is not None:
            kwargs["command"] = command
        if volumes:
            kwargs["volumes"] = volumes
        uid = os.geteuid()
        if uid != 0:
            # Files the drill database writes must stay deletable by Arkive.
            kwargs["user"] = f"{uid}:{os.getegid()}"
        return docker.containers.create(**kwargs)

    @staticmethod
    def _build_dump_tar(dump_path: Path, work: Path) -> Path:
        """Tar the dump as CONTAINER_DUMP_FILE for put_archive (no shell, no path interpolation)."""
        tar_path = work / "dump.tar"
        with tarfile.open(tar_path, "w") as tar:
            dir_info = tarfile.TarInfo(CONTAINER_DUMP_DIR.lstrip("/"))
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            dir_info.mtime = int(time.time())
            tar.addfile(dir_info)
            file_info = tarfile.TarInfo(CONTAINER_DUMP_FILE.lstrip("/"))
            file_info.size = dump_path.stat().st_size
            file_info.mode = 0o644
            file_info.mtime = int(time.time())
            with open(dump_path, "rb") as fh:
                tar.addfile(file_info, fh)
        try:
            dump_path.unlink()  # the tar now holds the only needed copy
        except OSError:
            pass
        return tar_path

    @staticmethod
    def _exec(container, cmd: list[str], env: dict | None = None) -> tuple[int | None, str]:
        """Short command; non-streaming exec_run reports the real exit code."""
        result = container.exec_run(cmd, environment=env or None)
        output = result.output if hasattr(result, "output") else result[1]
        code = result.exit_code if hasattr(result, "exit_code") else result[0]
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return code, output or ""

    def _exec_long(self, container, cmd: list[str], env: dict, timeout: float) -> int | None:
        """Long-running command: detached exec + exec_inspect polling.

        A streamed exec would hit the Docker client's read timeout while psql
        runs silently for minutes, and exec_run(stream=True) never reports an
        exit code at all.
        """
        api = getattr(getattr(container, "client", None), "api", None)
        if api is None or not hasattr(api, "exec_create"):
            return self._exec(container, cmd, env)[0]
        exec_id = api.exec_create(container.id, cmd, stdout=True, stderr=True, environment=env)["Id"]
        api.exec_start(exec_id, detach=True)
        deadline = time.monotonic() + timeout
        not_started_since = None
        while True:
            info = api.exec_inspect(exec_id) or {}
            if not info.get("Running"):
                if info.get("ExitCode") is not None:
                    return info["ExitCode"]
                not_started_since = not_started_since or time.monotonic()
                if time.monotonic() - not_started_since > 30:
                    return None
            if time.monotonic() >= deadline:
                raise DrillFailure(f"load did not finish within {int(timeout // 60)} minutes")
            time.sleep(self.poll_seconds)

    def _resolve_mysql_binaries(self, container, spec: _ServerSpec) -> bool:
        for attr, names in (("client", ("mariadb", "mysql")), ("admin", ("mariadb-admin", "mysqladmin"))):
            if getattr(spec, attr):
                continue
            for name in names:
                try:
                    code, _ = self._exec(container, [name, "--version"])
                except Exception:
                    return False
                if code == 0:
                    setattr(spec, attr, name)
                    break
        return bool(spec.client and spec.admin)

    def _wait_ready(self, container, spec: _ServerSpec) -> tuple[bool, str]:
        deadline = time.monotonic() + self.ready_timeout_seconds
        while True:
            try:
                container.reload()
            except Exception as exc:
                return False, f"container vanished: {exc}"
            status = getattr(container, "status", "")
            if status in ("exited", "dead"):
                return False, f"container exited: {self._log_tail(container)}"
            if status == "running":
                try:
                    if spec.is_postgres:
                        code, _ = self._exec(
                            container, ["pg_isready", "-q", "-h", "127.0.0.1", "-U", spec.pg_user, "-d", "postgres"]
                        )
                    elif self._resolve_mysql_binaries(container, spec):
                        code, _ = self._exec(
                            container,
                            [spec.admin, "ping", "-h", "127.0.0.1", "--protocol=tcp", "-uroot"],
                            {"MYSQL_PWD": spec.password},
                        )
                    else:
                        code = None
                except Exception:
                    code = None
                if code == 0:
                    return True, ""
            if time.monotonic() >= deadline:
                if not spec.is_postgres and not (spec.client and spec.admin):
                    return False, "no mysql/mariadb client found in the image"
                return False, (f"not ready after {int(self.ready_timeout_seconds)}s: {self._log_tail(container)}")
            time.sleep(self.poll_seconds)

    def _load_timeout(self, size_bytes: int) -> float:
        return min(
            self.max_load_timeout_seconds,
            max(self.min_load_timeout_seconds, self.min_load_timeout_seconds + size_bytes / MIB),
        )

    def _load_and_check(
        self, container, spec: _ServerSpec, prepared: PreparedDump, cand: DumpCandidate
    ) -> tuple[str, str]:
        timeout = self._load_timeout(prepared.size_bytes)
        load_started = time.monotonic()
        if spec.is_postgres:
            # The manifest / history name is authoritative; the dump's own
            # \\connect line is the fallback.
            database = next((name for name in (cand.db_name, prepared.database) if valid_pg_dbname(name)), None)
            if database is None:
                raise DrillFailure("could not determine which database the dump creates")
            maintenance_db = "template1" if database == "postgres" else "postgres"
            env = {
                "PGPASSWORD": spec.password,
                "ARKIVE_DRILL_USER": spec.pg_user,
                "ARKIVE_DRILL_MAINTDB": maintenance_db,
            }
            code = self._exec_long(container, ["sh", "-c", _PG_LOAD_SCRIPT], env, timeout)
            tool = "psql"
        else:
            env = {"MYSQL_PWD": spec.password, "ARKIVE_DRILL_CLIENT": spec.client or "mysql"}
            code = self._exec_long(container, ["sh", "-c", _MYSQL_LOAD_SCRIPT], env, timeout)
            tool = spec.client or "mysql"
        load_seconds = int(time.monotonic() - load_started)
        if code != 0:
            raise DrillFailure(f"{tool} load exited with code {code}: {self._load_errors(container)}")

        if spec.is_postgres:
            code, output = self._exec(
                container,
                ["psql", "-X", "-tA", "-h", "127.0.0.1", "-U", spec.pg_user, "-d", database, "-c", _PG_COUNT_SQL],
                {"PGPASSWORD": spec.password},
            )
        else:
            code, output = self._exec(
                container,
                [
                    spec.client or "mysql",
                    "-h",
                    "127.0.0.1",
                    "--protocol=tcp",
                    "-uroot",
                    "-N",
                    "-B",
                    "-e",
                    _MYSQL_COUNT_SQL,
                ],
                {"MYSQL_PWD": spec.password},
            )
        numbers = re.findall(r"^\s*(\d+)\s*$", output, re.MULTILINE)
        if code != 0 or not numbers:
            raise DrillFailure(f"could not count restored tables: {output.strip()[-300:] or code}")
        restored = int(numbers[-1])
        expected = prepared.create_tables
        size_note = f"{_human_size(prepared.size_bytes)} loaded in {load_seconds}s into {spec.image}"
        if expected == 0:
            return "passed", f"dump declares no tables; loaded cleanly ({size_note})"
        if restored == 0:
            raise DrillFailure(f"load finished but no tables were restored (dump declares {expected})")
        if restored < expected:
            raise DrillFailure(f"only {restored} of {expected} tables were restored")
        extra = f" (dump declares {expected})" if restored != expected else ""
        return "passed", f"{restored} tables restored{extra}; {size_note}"

    def _load_errors(self, container) -> str:
        try:
            _code, output = self._exec(container, ["tail", "-c", "1500", CONTAINER_LOAD_ERR])
        except Exception:
            output = ""
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        errors = [ln for ln in lines if "ERROR" in ln or "error" in ln.lower()] or lines
        return redact_credentials(" | ".join(errors[-3:]))[:600] or "no error output"

    @staticmethod
    def _log_tail(container) -> str:
        try:
            raw = container.logs(tail=15)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            return "no logs"
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return redact_credentials(" | ".join(lines[-4:]))[:600] or "no logs"

    @staticmethod
    def _remove_container(container) -> None:
        try:
            container.remove(force=True, v=True)
        except Exception as exc:
            logger.warning(
                "Could not remove drill container %s: %s",
                getattr(container, "name", getattr(container, "id", "?")),
                exc,
            )

    # ---- startup sweep helpers ----

    def _remove_stale_dirs(self) -> None:
        root = Path(self.config.restore_dir)
        if not root.is_dir():
            return
        for entry in root.iterdir():
            # Only directories this engine created: exact name format AND the
            # ownership marker naming the same drill. A user restore into e.g.
            # restore_dir/drill-recovery is never touched.
            match = _DRILL_DIR_RE.match(entry.name)
            if not match or entry.is_symlink() or not entry.is_dir():
                continue
            if match.group(1) == self._current_drill_id:
                continue
            try:
                owner = (entry / DRILL_MARKER).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if owner != match.group(1):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            logger.info("Removed stale drill directory %s", entry)

    def _remove_leftover_containers_blocking(self) -> int:
        removed = 0
        for container in self.docker_client.containers.list(all=True, filters={"label": DRILL_LABEL}):
            labels = getattr(container, "labels", None) or {}
            if self._current_drill_id and labels.get(DRILL_LABEL) == self._current_drill_id:
                continue
            self._remove_container(container)
            removed += 1
        if removed:
            logger.warning("Removed %d leftover restore drill container(s)", removed)
        return removed

    # ---- data access ----

    async def _wait_until_idle(self) -> bool:
        deadline = time.monotonic() + self.idle_wait_seconds
        while True:
            try:
                self.lock_manager.cleanup_stale_backup_lock()
                busy = self.lock_manager.is_backup_running() or self.lock_manager.is_restore_running()
            except Exception as exc:
                logger.debug("Lock check failed: %s", exc)
                busy = False
            if not busy:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.idle_poll_seconds)

    async def _load_targets(self) -> list[dict]:
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1 ORDER BY created_at, rowid")
            rows = [dict(r) for r in await cursor.fetchall()]
        # Local targets restore fastest; otherwise keep the configured order.
        rows.sort(key=lambda t: 0 if t.get("type") == "local" else 1)
        targets = []
        for row in rows:
            try:
                row["config"] = decrypt_config(row.get("config") or "{}", str(self.config.config_dir))
            except Exception as exc:
                logger.warning("Drill: cannot decrypt config of target %s: %s", row.get("name"), exc)
                continue
            targets.append(row)
        return targets

    async def _find_dump_snapshot(self, targets: list[dict]):
        dump_dir = os.path.normpath(str(self.config.dump_dir))

        def covers(paths) -> bool:
            for p in paths or []:
                norm = os.path.normpath(str(p))
                if norm == dump_dir or dump_dir.startswith(norm.rstrip("/") + "/"):
                    return True
            return False

        errors: list[str] = []
        for target in targets:
            name = target.get("name") or target.get("id")
            try:
                snapshots = await self._list_snapshots(target)
            except Exception as exc:
                # An unreachable/locked/corrupt repository is not an empty one.
                logger.warning("Drill: listing snapshots on %s failed: %s", name, exc)
                errors.append(f"{name}: {exc}")
                continue
            matching = [s for s in snapshots or [] if isinstance(s, dict) and covers(s.get("paths"))]
            if not matching:
                continue
            snap = matching[-1]  # restic lists snapshots oldest first
            snapshot_id = str(snap.get("id") or snap.get("short_id") or "")
            if not snapshot_id:
                continue
            short_id = str(snap.get("short_id") or snapshot_id[:8])
            try:
                # strict: a failed listing raises instead of looking like an
                # empty dump directory (whatever path the snapshot covers).
                entries = await self.backup_engine.ls(target, snapshot_id, dump_dir, strict=True)
            except ResticError as exc:
                error = DrillRepositoryError(f"could not list {dump_dir} in snapshot {short_id} on {name}: {exc}")
                error.target, error.snapshot_id = target, short_id
                raise error from exc
            return target, snapshot_id, short_id, entries or []
        if errors:
            raise DrillRepositoryError("could not list snapshots: " + "; ".join(errors))
        return None

    async def _list_snapshots(self, target: dict) -> list[dict]:
        """Snapshots of ``target``; raises when restic fails (never [] on error)."""
        lister = getattr(self.backup_engine, "list_snapshots", None)
        if lister is None:
            return await self.backup_engine.snapshots(target)
        return await lister(target)

    async def _read_manifest(self, target: dict, snapshot_id: str, short_id: str, drill_dir: Path) -> dict | None:
        """The dump manifest stored in the snapshot.

        Raises DrillRepositoryError when it cannot be restored or is corrupt
        (the repository is what a drill tests). Returns None only for a
        manifest in a format this version doesn't understand (inference is
        used instead).
        """
        dump_dir = str(self.config.dump_dir).rstrip("/")
        restore_to = drill_dir / "manifest"

        def failure(message: str) -> DrillRepositoryError:
            error = DrillRepositoryError(message)
            error.target, error.snapshot_id = target, short_id
            return error

        try:
            await asyncio.to_thread(restore_to.mkdir, parents=True, exist_ok=True)
            result = await self.backup_engine.restore(
                target=target,
                snapshot_id=snapshot_id,
                paths=[f"{dump_dir}/{DUMP_MANIFEST_NAME}"],
                restore_to=str(restore_to),
            )
            if result.get("status") != "success":
                raise failure(
                    f"restoring the dump manifest from snapshot {short_id} failed: "
                    f"{result.get('error') or 'restic returned an error'}"
                )
            path = restore_to / dump_dir.lstrip("/") / DUMP_MANIFEST_NAME

            def _load() -> dict | None:
                if not path.is_file():
                    raise failure(f"the dump manifest is missing after restoring it from snapshot {short_id}")
                if path.stat().st_size > MAX_MANIFEST_BYTES:
                    logger.warning("Drill: dump manifest is implausibly large; inferring from history")
                    return None
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise failure(f"the dump manifest in snapshot {short_id} is unreadable: {exc}") from exc
                if isinstance(data, dict) and isinstance(data.get("dumps"), list):
                    return data
                logger.warning("Drill: unrecognised dump manifest format; inferring from history")
                return None

            return await asyncio.to_thread(_load)
        finally:
            await asyncio.to_thread(shutil.rmtree, restore_to, True)

    async def _known_databases(self) -> tuple[list[dict], dict[str, str]]:
        known: list[dict] = []
        images: dict[str, str] = {}
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """SELECT DISTINCT container_name, db_type, db_name FROM job_run_databases
                       WHERE run_id IN (
                           SELECT jr.id FROM job_runs jr
                           WHERE EXISTS (SELECT 1 FROM job_run_databases d WHERE d.run_id = jr.id)
                           ORDER BY jr.started_at DESC LIMIT 20)"""
                )
                known.extend(dict(r) for r in await cursor.fetchall())
                cursor = await db.execute("SELECT name, image, databases FROM discovered_containers")
                for row in await cursor.fetchall():
                    if row["image"]:
                        images[row["name"]] = row["image"]
                    try:
                        entries = json.loads(row["databases"] or "[]")
                    except (TypeError, ValueError):
                        entries = []
                    for entry in entries:
                        if isinstance(entry, dict):
                            known.append({k: entry.get(k) for k in ("container_name", "db_type", "db_name")})
        except Exception as exc:
            logger.warning("Drill: could not read database history: %s", exc)
        return known, images

    async def _drill_history(self) -> dict[tuple[str, str], tuple[str, str]]:
        history: dict[tuple[str, str], tuple[str, str]] = {}
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute(
                    """SELECT r.container_name, COALESCE(r.db_name, ''), r.status, d.started_at
                       FROM restore_drill_results r JOIN restore_drills d ON d.id = r.drill_id
                       WHERE r.status IN ('passed', 'failed')
                       ORDER BY d.started_at, r.id"""
                )
                for container, db_name, status, started_at in await cursor.fetchall():
                    history[(container, db_name)] = (started_at or "", status)
        except Exception as exc:
            logger.warning("Drill: could not read drill history: %s", exc)
        return history

    async def _resolve_host_restore_dir(self) -> str | None:
        restore_dir = str(self.config.restore_dir)
        if not running_in_container():
            return restore_dir
        if self.docker_client is None:
            return None
        try:
            host = await asyncio.to_thread(host_path_for, restore_dir, self.docker_client)
        except Exception as exc:
            logger.debug("Host path lookup failed: %s", exc)
            return None
        if host and host.startswith("/var/lib/docker/"):
            # A named/anonymous volume lives in Docker's own storage anyway.
            return None
        return host

    async def _persist(self, drill_id: str, outcome: DrillOutcome, duration: int) -> None:
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """UPDATE restore_drills
                       SET status = ?, target_id = ?, snapshot_id = ?, completed_at = ?,
                           duration_seconds = ?, databases_tested = ?, databases_passed = ?,
                           databases_failed = ?, error_message = ?
                       WHERE id = ?""",
                    (
                        outcome.status,
                        outcome.target_id,
                        outcome.snapshot_id,
                        _now(),
                        duration,
                        len(outcome.tested),
                        outcome.passed,
                        outcome.failed,
                        outcome.error_message,
                        drill_id,
                    ),
                )
                await db.executemany(
                    """INSERT INTO restore_drill_results
                       (drill_id, container_name, db_type, db_name, dump_file, dump_size_bytes,
                        status, detail, duration_seconds)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            drill_id,
                            r.container_name,
                            r.db_type or "unknown",
                            r.db_name,
                            r.dump_file,
                            r.dump_size_bytes or 0,
                            r.status,
                            r.detail,
                            r.duration_seconds,
                        )
                        for r in outcome.results
                    ],
                )
                await db.commit()
        except Exception as exc:
            logger.error("Could not persist restore drill %s: %s", drill_id, exc)

    async def _record_activity(self, drill_id: str, outcome: DrillOutcome, duration: int) -> None:
        tested = len(outcome.tested)
        if outcome.status == "passed":
            message = f"Restore drill passed: {outcome.passed} database(s) restored from snapshot {outcome.snapshot_id}"
            severity = "info"
        elif outcome.status == "failed":
            message = f"Restore drill failed: {outcome.error_message or f'{outcome.failed} of {tested} failed'}"
            severity = "error"
        else:
            message = f"Restore drill skipped: {outcome.error_message or 'nothing to test'}"
            severity = "warning"
        details = {
            "drill_id": drill_id,
            "status": outcome.status,
            "target_id": outcome.target_id,
            "snapshot_id": outcome.snapshot_id,
            "duration_seconds": duration,
            "databases_tested": tested,
            "databases_passed": outcome.passed,
            "databases_failed": outcome.failed,
            "results": [
                {
                    "container_name": r.container_name,
                    "db_name": r.db_name,
                    "db_type": r.db_type,
                    "status": r.status,
                    "detail": r.detail[:300],
                }
                for r in outcome.results
            ][:50],
        }
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                await log_activity(db, "system", "restore_drill", message[:500], details, severity=severity)
        except Exception as exc:
            logger.warning("Could not record restore drill activity: %s", exc)

    async def _notify_failure(self, outcome: DrillOutcome) -> None:
        if not self.notifier:
            return
        failures = [r for r in outcome.results if r.status == "failed"]
        lines = [
            f"- {r.container_name}/{r.db_name or r.dump_file} ({r.db_type}): {r.detail[:300]}" for r in failures[:10]
        ]
        body = (
            f"{len(failures)} of {len(outcome.tested)} database dump(s) from snapshot "
            f"{outcome.snapshot_id} on {outcome.target_name} could not be restored."
        )
        if lines:
            body += "\n" + "\n".join(lines)
        elif outcome.error_message:
            body = outcome.error_message
        try:
            await self.notifier.send(
                "drill.failed", "Restore Drill Failed", body, severity="error", dedupe_key="restore_drill"
            )
        except Exception as exc:
            logger.warning("Failed to send restore drill notification: %s", exc)

    async def _publish(self, event: str, payload: dict) -> None:
        if self.event_bus is None:
            return
        try:
            result = self.event_bus.publish(event, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.debug("Could not publish %s: %s", event, exc)
