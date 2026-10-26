"""Database dump engine — safe dumps for Postgres, SQLite, MariaDB, MongoDB, Redis.

Logical dumps (pg_dump, mysqldump, mongodump) are written as plain files by
default (``dump_compression = "none"``): the restic repository already
compresses with zstd and, unlike a gzip stream, a plain dump deduplicates
between runs. ``dump_compression = "gzip"`` pipes the stream through
``gzip --rsyncable``, which still lets restic reuse unchanged regions.

Dump files are named ``<dump_prefix(...)>_<UTC YYYYmmdd_HHMMSS><ext>``; the
prefix carries a hash of the database identity so distinct databases never
share a cleanup group. Dumps are written to ``<final>.<rand>.partial`` and
renamed into place only after they were validated, so an interrupted dump can
never become "the newest dump" that cleanup keeps.

After every run the dump directory carries ``arkive-dumps-manifest.json``
describing each dump file (source container/image, database, kind,
compression, size, integrity), see ``write_dump_manifest``.
"""

import asyncio
import functools
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 — fixed argv, no shell
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import docker

from app import __version__
from app.core.config import ArkiveConfig
from app.models.discovery import DiscoveredDatabase
from app.services.settings_store import DEFAULTS as SETTING_DEFAULTS
from app.services.settings_store import get_setting
from app.utils.redact import redact_credentials
from app.utils.subprocess_runner import run_command

logger = logging.getLogger("arkive.db_dumper")

# Trailers written by the dump tools as their very last output. A dump that
# lacks its trailer was cut short (connection drop, OOM kill, timeout).
PG_DUMP_TRAILER = b"PostgreSQL database dump complete"
PG_DUMPALL_TRAILER = b"PostgreSQL database cluster dump complete"
MYSQL_DUMP_TRAILER = b"-- Dump completed"
DUMP_TRAILERS = (PG_DUMP_TRAILER, PG_DUMPALL_TRAILER, MYSQL_DUMP_TRAILER)
_TAIL_BYTES = 4096

DUMP_COMPRESSIONS = ("none", "gzip")
DUMP_MANIFEST_NAME = "arkive-dumps-manifest.json"
DUMP_MANIFEST_VERSION = 1
# db_name / file label of the per-cluster Postgres roles dump.
GLOBALS_DB_NAME = "globals"
_MANIFEST_INTEGRITY = {"ok", "failed", "skipped", "unverifiable"}

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
# elsewhere (flash backup retention, Arkive self-backup, the dump manifest).
_NON_DUMP_PREFIXES = ("flash_",)
_NON_DUMP_FILES = {"arkive_self.db", DUMP_MANIFEST_NAME}
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_DUMP_TS_PATTERN = re.compile(r"^(.+?)_(\d{8}_\d{6})\.")
# New-style prefixes end in "-<8 hex identity hash>"; older dumps lack it.
_HASHED_PREFIX = re.compile(r"^(?P<base>.+)-[0-9a-f]{8}$")
_MANIFEST_TMP_PREFIX = f".{DUMP_MANIFEST_NAME}."
_STALE_TMP_SECONDS = 3600
PARTIAL_SUFFIX = ".partial"
_STALE_PARTIAL_SECONDS = 6 * 3600

_manifest_lock = threading.Lock()


def safe_name(value: str) -> str:
    """File-name-safe form of a container or database name (as used in dump file names)."""
    return re.sub(r"[^a-zA-Z0-9_.\-]", "_", value)


def postgres_user_from_env(env: dict[str, str]) -> str:
    """The role Arkive connects as for a Postgres container (from its environment)."""
    return env.get("POSTGRES_USER") or env.get("DB_USERNAME") or env.get("DB_USER") or "postgres"


def dump_prefix(
    container_name: str, db_type: str, db_name: str, host_path: str | None = None, kind: str = "database"
) -> str:
    """Grouping prefix of a database's dump files: ``<safe container>_<safe label>-<hash8>``.

    Dump files are ``<prefix>_<UTC timestamp><ext>`` and ``cleanup_dump_dir``
    groups (and protects) by exactly this prefix. The hash covers the full
    identity, so databases whose names collapse to the same file-safe form
    (``sales data`` vs ``sales_data``; container ``a`` + db ``b_c`` vs ``a_b`` +
    ``c``; same-basename SQLite files) never share a group, and the roles dump
    (``kind="globals"``) never collides with a database named ``globals``.
    ``mysql`` and ``mariadb`` are the same type here (dumps report ``mariadb``).
    """
    db_type = "mariadb" if db_type == "mysql" else db_type
    identity = "\0".join([container_name, db_type, db_name, host_path or ""])
    if kind != "database":
        identity = f"{kind}\0{identity}"
    digest = hashlib.sha1(identity.encode()).hexdigest()[:8]  # nosec B324 — file naming, not security
    label = GLOBALS_DB_NAME if kind == "globals" else db_name
    return f"{safe_name(container_name)}_{safe_name(label)}-{digest}"


def legacy_dump_prefix(container_name: str, db_name: str) -> str:
    """Grouping prefix a database's dumps had before names carried the identity
    hash (``<safe container>_<safe db name>``; databases only — older releases
    had no roles dump). Several identities can share one legacy prefix."""
    return f"{safe_name(container_name)}_{safe_name(db_name)}"


def _prefix_base(prefix: str) -> str | None:
    """``<safe container>_<safe label>`` of a hashed prefix; None for an old-style prefix."""
    match = _HASHED_PREFIX.match(prefix)
    return match.group("base") if match else None


def dump_compression_of(file_name: str) -> str:
    return "gzip" if file_name.endswith(".gz") else "none"


def is_dump_file_name(name: str) -> bool:
    """True for finished per-database dump files (not flash archives, sidecars,
    in-progress ``.partial`` files, the manifest...)."""
    if name.startswith(".") or name in _NON_DUMP_FILES or name.startswith(_NON_DUMP_PREFIXES):
        return False
    return not name.endswith((PARTIAL_SUFFIX, *_SQLITE_SIDECAR_SUFFIXES))


def is_arkive_dump_name(name: str) -> bool:
    """True when ``name`` follows Arkive's ``<container>_<db>_<YYYYmmdd_HHMMSS>.<ext>`` scheme."""
    return bool(_DUMP_TS_PATTERN.match(name))


@dataclass
class StreamResult:
    exit_code: int | None
    bytes_written: int
    stderr: str
    tail: bytes


# ---------------------------------------------------------------------------
# Dump file writers
# ---------------------------------------------------------------------------


class _PlainDumpWriter:
    """Writes the stream unchanged; restic compresses and deduplicates it."""

    def __init__(self, path: str):
        self._file = open(path, "wb")

    def write(self, data: bytes) -> None:
        self._file.write(data)

    def close(self) -> None:
        self._file.close()

    def abort(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


class _PythonGzipDumpWriter(_PlainDumpWriter):
    """Fallback when no gzip binary with --rsyncable is available. Each run
    produces an entirely new byte stream, so restic cannot deduplicate it."""

    def __init__(self, path: str):
        self._file = gzip.open(path, "wb", compresslevel=6)


class _RsyncableGzipDumpWriter:
    """Pipes the stream through ``gzip --rsyncable``.

    --rsyncable resets the compressor at content-defined points, so a local
    change only alters the compressed output around it and restic can still
    reuse the unchanged chunks of the previous dump.
    """

    def __init__(self, path: str, gzip_binary: str):
        self._out = open(path, "wb")
        try:
            self._proc = subprocess.Popen(  # nosec B603 — fixed argv
                [gzip_binary, "--rsyncable", "-6", "-c"],
                stdin=subprocess.PIPE,
                stdout=self._out,
                stderr=subprocess.PIPE,
            )
        except BaseException:
            self._out.close()
            raise

    def _stderr(self) -> str:
        try:
            return self._proc.stderr.read().decode(errors="replace").strip()[:300]
        except Exception:
            return ""

    def write(self, data: bytes) -> None:
        try:
            self._proc.stdin.write(data)
        except BrokenPipeError:
            self._proc.wait()
            raise RuntimeError(f"gzip exited early (code {self._proc.returncode}): {self._stderr()}")

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except BrokenPipeError:
            pass
        err = self._stderr()
        code = self._proc.wait()
        self._out.close()
        if code != 0:
            raise RuntimeError(f"gzip exited with code {code}: {err or 'no output'}")

    def abort(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass
        for stream in (self._proc.stdin, self._proc.stderr, self._out):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._proc.wait(timeout=10)
        except Exception:
            pass


@functools.lru_cache(maxsize=1)
def _rsyncable_gzip_binary() -> str | None:
    """Path of a gzip binary that supports --rsyncable (GNU gzip), else None."""
    binary = shutil.which("gzip")
    if not binary:
        logger.warning("gzip binary not found; gzip dumps fall back to Python gzip (no restic dedup)")
        return None
    try:
        probe = subprocess.run(  # nosec B603 — fixed argv
            [binary, "--rsyncable", "-c"],
            input=b"",
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        logger.warning("gzip --rsyncable probe failed (%s); using Python gzip", e)
        return None
    if probe.returncode != 0:
        logger.warning("%s does not support --rsyncable; using Python gzip", binary)
        return None
    return binary


def _open_dump_writer(path: str, compression: str):
    if compression != "gzip":
        return _PlainDumpWriter(path)
    binary = _rsyncable_gzip_binary()
    if binary:
        try:
            return _RsyncableGzipDumpWriter(path, binary)
        except OSError as e:
            logger.warning("Could not start gzip --rsyncable (%s); using Python gzip", e)
    return _PythonGzipDumpWriter(path)


# ---------------------------------------------------------------------------
# Dump manifest
# ---------------------------------------------------------------------------


def _utc_iso(ts: float | None = None) -> str:
    moment = datetime.now(UTC) if ts is None else datetime.fromtimestamp(ts, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _created_at(name: str, mtime: float) -> str:
    """Dump start time from the file name's UTC timestamp, else the file mtime."""
    match = _DUMP_TS_PATTERN.match(name)
    if match:
        try:
            parsed = datetime.strptime(match.group(2), "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return _utc_iso(mtime)


def _empty_manifest() -> dict:
    return {"version": DUMP_MANIFEST_VERSION, "generated_at": None, "arkive_version": None, "dumps": []}


def read_dump_manifest(dump_dir) -> dict:
    """Return the dump manifest of ``dump_dir``.

    A missing or unreadable manifest yields an empty one (``dumps == []``).
    Shape: ``{"version": 1, "generated_at", "arkive_version", "dumps": [{"file",
    "container_name", "image", "db_type", "db_name", "host_path", "kind",
    "compression", "size_bytes", "created_at", "integrity"}]}``.
    """
    path = Path(dump_dir) / DUMP_MANIFEST_NAME
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty_manifest()
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable dump manifest %s: %s", path, e)
        return _empty_manifest()
    if not isinstance(data, dict) or not isinstance(data.get("dumps"), list):
        logger.warning("Ignoring malformed dump manifest %s", path)
        return _empty_manifest()
    data["dumps"] = [e for e in data["dumps"] if isinstance(e, dict) and isinstance(e.get("file"), str)]
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_head(path: Path, size: int = 2048) -> bytes:
    try:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rb") as handle:
            return handle.read(size)
    except Exception:
        return b""


def _infer_manifest_entry(path: Path, stat: os.stat_result, known_containers) -> dict:
    """Best-effort entry for a dump file with no recorded metadata (e.g. written
    by an Arkive version before the manifest existed). ``container_name`` and
    ``db_name`` stay empty when they cannot be derived unambiguously."""
    name = path.name
    match = _DUMP_TS_PATTERN.match(name)
    prefix = match.group(1) if match else name.split(".", 1)[0]
    # "<container>_<label>-<hash>" (current) or "<container>_<label>" (older).
    prefix = _prefix_base(prefix) or prefix
    kind = "database"
    db_type = "unknown"
    if name.endswith((".sql", ".sql.gz")):
        head = _read_head(path)
        if b"PostgreSQL database cluster dump" in head:
            db_type, kind = "postgres", "globals"
        elif b"PostgreSQL database dump" in head:
            db_type = "postgres"
        elif b"MySQL dump" in head or b"MariaDB dump" in head:
            db_type = "mariadb"
    elif name.endswith(".sqlite3"):
        db_type = "sqlite"
    elif name.endswith((".archive", ".archive.gz")):
        db_type = "mongodb"
    elif name.endswith(".rdb"):
        db_type = "redis"

    container_name = ""
    db_name = ""
    for container in sorted(known_containers, key=len, reverse=True):
        safe_container = safe_name(container)
        if prefix.startswith(safe_container + "_"):
            container_name = container
            db_name = prefix[len(safe_container) + 1 :]
            break
    if not container_name:
        for marker, kind_or_type in ((f"_{GLOBALS_DB_NAME}", "globals"), ("_redis", "redis")):
            if prefix.endswith(marker) and kind_or_type in (kind, db_type):
                container_name = prefix[: -len(marker)]
    if kind == "globals":
        db_name = GLOBALS_DB_NAME
    elif db_type == "redis":
        db_name = "redis"

    return {
        "file": name,
        "container_name": container_name,
        "image": "",
        "db_type": db_type,
        "db_name": db_name,
        "host_path": None,
        "kind": kind,
        "compression": dump_compression_of(name),
        "size_bytes": stat.st_size,
        "created_at": _created_at(name, stat.st_mtime),
        "integrity": "skipped",
    }


def write_dump_manifest(dump_dir, new_entries: list[dict] | None = None, known_containers=()) -> dict:
    """Rewrite ``arkive-dumps-manifest.json`` (atomically) for the files now in ``dump_dir``.

    Each dump file present gets an entry: from ``new_entries`` (this run),
    else carried over from the previous manifest (size refreshed), else
    inferred from the file itself. Entries of files that are gone are dropped.
    """
    dump_dir = Path(dump_dir)
    with _manifest_lock:
        previous = {e["file"]: e for e in read_dump_manifest(dump_dir)["dumps"]}
        fresh = {e["file"]: e for e in (new_entries or [])}
        entries = []
        for path in sorted(dump_dir.iterdir(), key=lambda p: p.name):
            if not is_dump_file_name(path.name):
                continue
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            entry = fresh.get(path.name) or previous.get(path.name)
            if entry is None:
                entry = _infer_manifest_entry(path, stat, known_containers)
            else:
                entry = {**entry, "size_bytes": stat.st_size}
            entries.append(entry)
        manifest = {
            "version": DUMP_MANIFEST_VERSION,
            "generated_at": _utc_iso(),
            "arkive_version": __version__,
            "dumps": entries,
        }
        _atomic_write_json(dump_dir / DUMP_MANIFEST_NAME, manifest)
    return manifest


def prune_dump_manifest(dump_dir) -> bool:
    """Drop manifest entries whose dump files no longer exist. Returns True if rewritten."""
    dump_dir = Path(dump_dir)
    if not (dump_dir / DUMP_MANIFEST_NAME).is_file():
        return False
    entries = read_dump_manifest(dump_dir)["dumps"]
    if all((dump_dir / e["file"]).is_file() for e in entries):
        return False
    write_dump_manifest(dump_dir)
    return True


def _dump_sort_key(path: Path) -> str:
    match = _DUMP_TS_PATTERN.match(path.name)
    return match.group(2) if match else path.name


def _remove_stale(path: Path, max_age_seconds: float) -> bool:
    try:
        if path.stat().st_mtime < time.time() - max_age_seconds:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def cleanup_dump_dir(
    dump_dir: Path,
    keep_last: int = 1,
    max_age_days: int | None = 7,
    protect_prefixes: set[str] | None = None,
    legacy_prefixes: dict[str, str] | None = None,
) -> int:
    """Prune the database dump directory.

    Dumps are grouped by their ``dump_prefix`` (regardless of format, so
    switching between ``.sql`` and ``.sql.gz`` leaves only the newest). Per
    group the newest ``keep_last`` dumps are kept. Dumps older than
    ``max_age_days`` are removed too (dumps of containers that no longer exist
    would otherwise be re-uploaded in every snapshot forever), except the
    newest dump of each group in ``protect_prefixes`` (databases that still
    exist keep their last good dump however old it is). ``max_age_days=None``
    disables age-based removal.

    Older dumps named without the identity hash (``<container>_<label>_<ts>``)
    group by that legacy prefix. ``legacy_prefixes`` maps the hashed prefix of
    each known database to its legacy prefix: a legacy group is retired once
    every database mapping to it has a newer hashed dump, and is protected like
    those databases until then. Legacy groups no known database maps to follow
    the plain age rule. A shared sanitised name alone never retires a file —
    a database named ``globals`` must not retire an old roles dump.

    In-progress ``.partial`` files are never treated as dumps; ones older than
    6 hours (left by a crash) are deleted. Orphaned SQLite
    ``-wal``/``-shm``/``-journal`` sidecars are removed with their base file.
    Flash archives, the Arkive self-backup and the dump manifest are left
    alone; the manifest is rewritten without the removed files.

    Returns the number of files removed.
    """
    if not dump_dir.is_dir():
        return 0
    keep_last = max(keep_last, 1)
    cutoff = time.time() - max_age_days * 86400 if max_age_days else None
    protected = set(protect_prefixes or ())
    # legacy prefix -> hashed prefixes of the known databases that used it
    legacy_owners: dict[str, list[str]] = {}
    for hashed, legacy in (legacy_prefixes or {}).items():
        legacy_owners.setdefault(legacy, []).append(hashed)

    groups: dict[str, list[Path]] = {}
    sidecars: list[Path] = []
    removed = 0
    for path in dump_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.startswith(_MANIFEST_TMP_PREFIX):
            # Left behind by a crash mid-write; never the live manifest.
            _remove_stale(path, _STALE_TMP_SECONDS)
            continue
        if name.endswith(PARTIAL_SUFFIX):
            # A dump still being written (or abandoned by a crash).
            if _remove_stale(path, _STALE_PARTIAL_SECONDS):
                removed += 1
                logger.info("Removed abandoned partial dump: %s", path)
            continue
        if name.startswith(".") or name in _NON_DUMP_FILES or name.startswith(_NON_DUMP_PREFIXES):
            continue
        if name.endswith(_SQLITE_SIDECAR_SUFFIXES):
            sidecars.append(path)
            continue
        match = _DUMP_TS_PATTERN.match(name)
        prefix = match.group(1) if match else name
        groups.setdefault(prefix, []).append(path)

    for files in groups.values():
        # Newest first by the timestamp in the name (extension-independent).
        files.sort(key=_dump_sort_key, reverse=True)

    def _newest(prefix: str) -> str:
        files = groups.get(prefix)
        return _dump_sort_key(files[0]) if files else ""

    to_remove: list[Path] = []
    for prefix, files in groups.items():
        owners = legacy_owners.get(prefix, [])
        is_protected = prefix in protected
        if owners and _prefix_base(prefix) is None:
            superseded = [_newest(owner) > _dump_sort_key(files[0]) for owner in owners]
            if all(superseded):
                to_remove.extend(files)  # every database here has a newer hashed dump
                continue
            is_protected = is_protected or any(
                owner in protected and not done for owner, done in zip(owners, superseded)
            )
        for index, path in enumerate(files):
            if index >= keep_last:
                to_remove.append(path)
            elif cutoff is not None and not (is_protected and index == 0):
                try:
                    if path.stat().st_mtime < cutoff:
                        to_remove.append(path)
                except OSError:
                    continue

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

    try:
        prune_dump_manifest(dump_dir)
    except Exception as e:
        logger.warning("Failed to update dump manifest after cleanup: %s", e)

    if removed:
        logger.info("Dump cleanup: removed %d old dump files", removed)
    return removed


def _sqlite_backup_ok(result, copy_path: str) -> bool:
    """sqlite3 can exit 0 while printing "Error: unable to open database file"."""
    if result.returncode != 0 or "error" in (result.stderr or "").lower():
        return False
    return os.path.exists(copy_path) and os.path.getsize(copy_path) > 0


def _file_state(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


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
    # "database", or "globals" for a Postgres cluster's roles/tablespaces
    # (pg_dumpall --globals-only, db_name "globals"); not a discovered database.
    kind: str = "database"
    # Finer integrity detail for the manifest, e.g. "unverifiable" when the
    # stock checker cannot evaluate an app-specific SQLite schema.
    integrity_detail: str | None = None


class DBDumper:
    """Dumps databases using native tools."""

    def __init__(self, docker_client, config: ArkiveConfig):
        self.docker = docker_client
        self.config = config
        self.dump_dir = config.dump_dir
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        # Output format for logical dumps; refreshed from the dump_compression
        # setting at the start of every dump_all / dump_single.
        self.compression = SETTING_DEFAULTS.get("dump_compression", "none")

    def _timestamp(self) -> str:
        return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def _safe(value: str) -> str:
        return safe_name(value)

    def _sql_ext(self) -> str:
        return ".sql.gz" if self.compression == "gzip" else ".sql"

    def _dump_path(self, db: DiscoveredDatabase, ts: str, ext: str, kind: str = "database") -> str:
        """Final dump file path: ``<dump_prefix>_<ts><ext>`` (unique per database identity)."""
        prefix = dump_prefix(db.container_name, db.db_type, db.db_name, db.host_path, kind=kind)
        return str(self.dump_dir / f"{prefix}_{ts}{ext}")

    @staticmethod
    def _partial_path(final_path: str) -> str:
        """Unique in-progress path next to ``final_path`` (renamed into place when valid)."""
        return f"{final_path}.{uuid.uuid4().hex[:8]}{PARTIAL_SUFFIX}"

    @staticmethod
    def _discard(path: str) -> None:
        """Remove an unfinished dump file (and SQLite sidecars); missing is fine."""
        for candidate in (path, *(path + suffix for suffix in _SQLITE_SIDECAR_SUFFIXES)):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Could not remove unfinished dump %s: %s", candidate, e)

    async def _load_compression(self) -> str:
        """Read the dump_compression setting; default on any problem."""
        default = SETTING_DEFAULTS.get("dump_compression", "none")
        db_path = getattr(self.config, "db_path", None)
        try:
            if not isinstance(db_path, (str, os.PathLike)):
                return default
            # Convert first: os.stat() would treat an object with __index__
            # (e.g. a mocked config attribute) as a file descriptor.
            db_file = os.fspath(db_path)
            if not isinstance(db_file, str) or not os.path.isfile(db_file):
                return default
            async with aiosqlite.connect(db_file) as db:
                value = await get_setting(db, "dump_compression", default)
        except Exception as e:
            logger.warning("Could not read dump_compression setting, using %r: %s", default, e)
            return default
        value = (value or "").strip().lower()
        if value not in DUMP_COMPRESSIONS:
            logger.warning("Unknown dump_compression %r, using %r", value, default)
            return default
        return value

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

    def _exec_stream_to_file(
        self,
        container,
        cmd: list[str],
        dump_path: str,
        environment: dict | None = None,
    ) -> StreamResult:
        """Run ``cmd`` in ``container`` and stream stdout into ``dump_path``
        (callers pass the ``.partial`` path and rename it once validated).

        The output is written chunk by chunk (never buffered whole) as plain
        bytes or through gzip, per ``self.compression``. The completion-trailer
        tail is taken from the uncompressed stream.

        docker-py's ``exec_run(stream=True)`` always reports ``exit_code=None``,
        which hid failed dumps. With a real Docker API client we use the
        low-level exec API so the exit code can be inspected after the stream
        ends; other clients fall back to ``exec_run``.
        """
        api = getattr(getattr(container, "client", None), "api", None)
        exec_id = None
        if isinstance(api, docker.APIClient):
            exec_id = api.exec_create(
                container.id,
                cmd,
                stdout=True,
                stderr=True,
                environment=environment or None,
            )["Id"]
            output = api.exec_start(exec_id, stream=True, demux=True)
            exit_code = None
        else:
            kwargs = {"environment": environment} if environment else {}
            exit_code, output = container.exec_run(cmd, demux=True, stream=True, **kwargs)

        bytes_written = 0
        tail = b""
        stderr_chunks: list[bytes] = []
        writer = _open_dump_writer(dump_path, self.compression)
        try:
            for chunk in output:
                if isinstance(chunk, tuple):
                    stdout_data, stderr_data = chunk
                else:
                    stdout_data, stderr_data = chunk, None
                if stdout_data:
                    writer.write(stdout_data)
                    bytes_written += len(stdout_data)
                    tail = (tail + stdout_data[-_TAIL_BYTES:])[-_TAIL_BYTES:]
                if stderr_data:
                    stderr_chunks.append(stderr_data)
            writer.close()
        except BaseException:
            writer.abort()
            raise

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
        self,
        db: DiscoveredDatabase,
        db_type: str,
        tool: str,
        dump_path: str,
        stream: StreamResult,
        trailer: bytes | None,
        partial_path: str | None = None,
    ) -> DumpResult:
        """Turn a streamed dump into a DumpResult, detecting failure and truncation.

        The stream was written to ``partial_path``; only a dump that passed
        every check is renamed to ``dump_path``. A failed one is left for the
        caller to discard.
        """

        def failed(error: str) -> DumpResult:
            return DumpResult(
                container_name=db.container_name,
                db_type=db_type,
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="failed" if stream.bytes_written else "skipped",
                status="failed",
                error=redact_credentials(error),
            )

        if stream.exit_code is not None and stream.exit_code != 0:
            logger.error(
                "%s for %s/%s failed (exit %s): %s",
                tool,
                db.container_name,
                db.db_name,
                stream.exit_code,
                redact_credentials(stream.stderr[:500]),
            )
            return failed(f"{tool} exited with code {stream.exit_code}: {stream.stderr[:500] or 'no output'}")

        if stream.bytes_written == 0:
            return failed(stream.stderr[:500] or "Empty dump file")

        integrity = "skipped"
        if trailer is not None:
            if trailer in stream.tail:
                integrity = "ok"
            else:
                logger.error(
                    "%s output for %s/%s is truncated (no completion trailer)", tool, db.container_name, db.db_name
                )
                return failed(
                    f"{tool} output is incomplete (missing completion trailer): "
                    f"{stream.stderr[:300] or 'dump was cut short'}"
                )

        if stream.bytes_written < 100:
            logger.warning(
                "%s for %s/%s produced suspiciously small output (%d bytes)",
                tool,
                db.container_name,
                db.db_name,
                stream.bytes_written,
            )
        if stream.stderr and (db_type != "mongodb" or "error" in stream.stderr.lower()):
            logger.warning(
                "%s stderr for %s/%s: %s", tool, db.container_name, db.db_name, redact_credentials(stream.stderr[:500])
            )

        if partial_path is not None:
            os.replace(partial_path, dump_path)
        size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
        return DumpResult(
            container_name=db.container_name,
            db_type=db_type,
            db_name=db.db_name,
            dump_path=dump_path,
            dump_size_bytes=size,
            integrity_check=integrity,
            status="success",
        )

    async def dump_all(self, databases: list[DiscoveredDatabase]) -> list[DumpResult]:
        """Dump all discovered databases. Continue on failure.

        Besides one result per database, every Postgres container gets one
        ``kind="globals"`` result (roles via pg_dumpall --globals-only). The
        dump manifest is rewritten afterwards.
        """
        databases = self.dedupe(databases)
        self.compression = await self._load_compression()
        results: list[DumpResult] = []
        host_paths: dict[int, str | None] = {}
        for db in databases:
            try:
                result = await self._dump_one(db)
                if result.status != "success":
                    # A failed/truncated dump must not shadow the previous
                    # good dump (cleanup keeps only the newest file).
                    self._remove_dump_file(result.dump_path)
            except Exception as e:
                error_str = str(e)
                redacted_error = redact_credentials(error_str)
                logger.error("Failed to dump %s/%s: %s", db.container_name, db.db_name, redacted_error)
                result = DumpResult(
                    container_name=db.container_name,
                    db_type=db.db_type,
                    db_name=db.db_name,
                    dump_path="",
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=redacted_error,
                )
            host_paths[id(result)] = db.host_path
            results.append(result)

        # Roles and tablespaces live outside any single database: dump them
        # once per Postgres container, connecting to a database known to work.
        clusters: dict[str, str] = {}
        connect_ok: dict[str, bool] = {}
        for db, result in zip(databases, results):
            if db.db_type != "postgres":
                continue
            ok = result.status == "success"
            if db.container_name not in clusters or (ok and not connect_ok[db.container_name]):
                clusters[db.container_name] = db.db_name
                connect_ok[db.container_name] = ok
        for container_name, connect_db in clusters.items():
            try:
                result = await asyncio.to_thread(self._dump_postgres_globals_blocking, container_name, connect_db)
            except Exception as e:
                result = DumpResult(
                    container_name=container_name,
                    db_type="postgres",
                    db_name=GLOBALS_DB_NAME,
                    dump_path="",
                    dump_size_bytes=0,
                    integrity_check="skipped",
                    status="failed",
                    error=redact_credentials(str(e)),
                    kind="globals",
                )
            if result.status != "success":
                self._remove_dump_file(result.dump_path)
            results.append(result)

        await self._update_manifest(results, host_paths, {db.container_name for db in databases})
        return results

    async def _update_manifest(
        self, results: list[DumpResult], host_paths: dict[int, str | None], known_containers: set[str]
    ) -> None:
        def build_and_write() -> None:
            images: dict[str, str] = {}
            entries = []
            for result in results:
                if result.status != "success" or not result.dump_path or not os.path.isfile(result.dump_path):
                    continue
                if result.container_name not in images:
                    images[result.container_name] = self._container_image(result.container_name)
                stat = os.stat(result.dump_path)
                name = os.path.basename(result.dump_path)
                integrity = result.integrity_detail or result.integrity_check
                entries.append(
                    {
                        "file": name,
                        "container_name": result.container_name,
                        "image": images[result.container_name],
                        "db_type": result.db_type,
                        "db_name": result.db_name,
                        "host_path": host_paths.get(id(result)),
                        "kind": result.kind,
                        "compression": dump_compression_of(name),
                        "size_bytes": stat.st_size,
                        "created_at": _created_at(name, stat.st_mtime),
                        "integrity": integrity if integrity in _MANIFEST_INTEGRITY else "skipped",
                    }
                )
            write_dump_manifest(self.dump_dir, entries, known_containers)

        try:
            await asyncio.to_thread(build_and_write)
        except Exception as e:
            logger.error("Failed to write dump manifest in %s: %s", self.dump_dir, e)

    def _container_image(self, container_name: str) -> str:
        """Image tag (or ID) of a container, '' when it cannot be determined."""
        try:
            container = self.docker.containers.get(container_name)
        except Exception:
            return ""
        image = getattr(container, "image", None)
        try:
            tags = getattr(image, "tags", None)
        except Exception:
            tags = None
        if isinstance(tags, (list, tuple)) and tags and isinstance(tags[0], str):
            return tags[0]
        attrs = getattr(container, "attrs", None)
        if isinstance(attrs, dict):
            config = attrs.get("Config")
            if isinstance(config, dict) and isinstance(config.get("Image"), str):
                return config["Image"]
            if isinstance(attrs.get("Image"), str):
                return attrs["Image"]
        image_id = getattr(image, "id", None)
        return image_id if isinstance(image_id, str) else ""

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
    def _sanitize_pg_dbname(value: str) -> str:
        """Validate a Postgres database name passed as one argv element.

        Commands run without a shell, so the only hazards are libpq treating
        the value as a connection string ("=" or a postgres:// URI) or as an
        option (leading "-"). Names enumerated from pg_database may contain
        spaces or capitals and must still be dumpable.
        """
        if not value or len(value.encode()) > 63 or value.startswith("-") or any(ch in value for ch in "=/\x00\r\n"):
            raise ValueError("Invalid database name: contains disallowed characters")
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
        dump_path = self._dump_path(db, self._timestamp(), self._sql_ext())
        partial = self._partial_path(dump_path)

        try:
            container = self.docker.containers.get(db.container_name)
            env = self._get_container_env(container)
            # Sanitize identifiers to prevent command injection
            pg_user = self._sanitize_identifier(postgres_user_from_env(env))
            db_name = self._sanitize_pg_dbname(db.db_name)
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
            stream = self._exec_stream_to_file(container, exec_cmd, partial)
            return self._streamed_dump_result(db, "postgres", "pg_dump", dump_path, stream, PG_DUMP_TRAILER, partial)
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="postgres",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=redact_credentials(str(e)),
            )
        finally:
            self._discard(partial)

    def _dump_postgres_globals_blocking(self, container_name: str, connect_db: str) -> DumpResult:
        """Dump a Postgres cluster's roles/tablespaces (pg_dumpall --globals-only).

        A non-superuser cannot read pg_authid; then the dump is retried with
        --no-role-passwords (roles restored without passwords). Any other
        failure is reported like a failed database dump.
        """
        pseudo_db = DiscoveredDatabase(container_name=container_name, db_type="postgres", db_name=GLOBALS_DB_NAME)
        dump_path = self._dump_path(pseudo_db, self._timestamp(), self._sql_ext(), kind="globals")
        partial = self._partial_path(dump_path)
        try:
            container = self.docker.containers.get(container_name)
            env = self._get_container_env(container)
            pg_user = self._sanitize_identifier(postgres_user_from_env(env))
            options = ["-U", pg_user, "-l", self._sanitize_pg_dbname(connect_db)]
            stream = self._exec_stream_to_file(container, ["pg_dumpall", "--globals-only", *options], partial)
            failed = (stream.exit_code not in (None, 0)) or PG_DUMPALL_TRAILER not in stream.tail
            if failed and "permission denied" in stream.stderr.lower():
                logger.warning(
                    "pg_dumpall --globals-only on %s: %s is not a superuser; retrying with "
                    "--no-role-passwords (roles will be restored without passwords)",
                    container_name,
                    pg_user,
                )
                stream = self._exec_stream_to_file(
                    container,
                    ["pg_dumpall", "--globals-only", "--no-role-passwords", *options],
                    partial,
                )
            result = self._streamed_dump_result(
                pseudo_db,
                "postgres",
                "pg_dumpall",
                dump_path,
                stream,
                PG_DUMPALL_TRAILER,
                partial,
            )
        except Exception as e:
            result = DumpResult(
                container_name=container_name,
                db_type="postgres",
                db_name=GLOBALS_DB_NAME,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=redact_credentials(str(e)),
            )
        finally:
            self._discard(partial)
        result.kind = "globals"
        return result

    # Attempts at an immutable copy before giving up on a file that keeps changing.
    sqlite_immutable_attempts = 3

    async def _sqlite_readonly_backup(self, host_path: str, partial: str, run_backup):
        """Copy a SQLite file that can only be opened read-only.

        ``mode=ro`` is consistent: it reads the write-ahead log and takes shared
        locks through the app's ``-shm`` file, which exists while the app has the
        database open. Without a ``-shm`` it can't open a WAL database at all,
        and the only option is ``immutable=1``, which ignores the WAL and takes
        no locks. That is only safe when the WAL holds nothing (the app closed
        the database and everything was checkpointed into the main file) and the
        file doesn't change during the copy — otherwise recent writes (possibly
        all of them) would be silently missing from a copy that still passes
        ``integrity_check``.

        Returns ``(result, None)`` on success or ``(None, error)``.
        """
        result = await run_backup(f"file:{host_path}?mode=ro")
        if _sqlite_backup_ok(result, partial):
            return result, None
        ro_error = (result.stderr or result.stdout or "").strip()

        wal = f"{host_path}-wal"
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            return None, (
                "cannot copy consistently: the database has uncheckpointed write-ahead-log changes "
                f"but could not be opened read-only ({ro_error or 'no error text'}). Give Arkive "
                "read-write access to this app's data, or the next run retries once the app is idle."
            )

        for _ in range(self.sqlite_immutable_attempts):
            before = _file_state(host_path)
            result = await run_backup(f"file:{host_path}?mode=ro&immutable=1")
            if not _sqlite_backup_ok(result, partial):
                return None, (result.stderr or result.stdout or ro_error or "sqlite3 .backup failed").strip()
            after = _file_state(host_path)
            wal_now = os.path.exists(wal) and os.path.getsize(wal) > 0
            if before == after and not wal_now:
                return result, None
            logger.info("SQLite file %s changed during an immutable copy; retrying", host_path)
        return None, (
            "cannot copy consistently: the database kept changing while it was copied without locks "
            "(read-only mount). Give Arkive read-write access to this app's data, or the next run retries."
        )

    async def _dump_sqlite(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump SQLite using HOST sqlite3 binary on bind-mounted path.

        The copy is made into a ``.partial`` file and renamed into place only
        when it passed ``PRAGMA integrity_check`` (or the check could not run
        because of an app-specific collation/extension). A corrupt copy is a
        failed dump, so the previous good dump is kept.
        """
        dump_path = self._dump_path(db, self._timestamp(), ".sqlite3")

        def failed(error: str, integrity: str = "skipped", path: str = "") -> DumpResult:
            return DumpResult(
                container_name=db.container_name,
                db_type="sqlite",
                db_name=db.db_name,
                dump_path=path,
                dump_size_bytes=0,
                integrity_check=integrity,
                status="failed",
                error=error,
            )

        if not db.host_path:
            return failed("No host path available for SQLite backup")

        # Validate host_path is absolute and does not contain traversal
        host_path = os.path.normpath(db.host_path)
        if not os.path.isabs(host_path) or ".." in db.host_path:
            return failed("Invalid host path: must be absolute with no traversal")

        partial = self._partial_path(dump_path)
        try:
            # Use host sqlite3 binary — NEVER cp
            # shlex.quote() prevents injection via dump_path containing spaces/special chars
            async def _run_backup(source: str):
                return await run_command(["sqlite3", source, f".backup {shlex.quote(partial)}"])

            result = await _run_backup(host_path)
            if not _sqlite_backup_ok(result, partial):
                # Typical on a read-only mount: SQLite can't open the file read-write.
                result, error = await self._sqlite_readonly_backup(host_path, partial, _run_backup)
                if result is None:
                    return failed(error, "failed", dump_path)

            # .backup preserves the source's WAL journal mode; switch the copy to a
            # rollback journal so it is a single self-contained file and opening it
            # (integrity check, restore) doesn't leave -wal/-shm sidecars behind.
            await run_command(["sqlite3", partial, "PRAGMA journal_mode=DELETE;"])

            # Integrity check
            integrity_detail = None
            check_result = await run_command(["sqlite3", partial, "PRAGMA integrity_check"])
            check_output = f"{check_result.stdout}\n{check_result.stderr}".lower()
            if any(marker in check_output for marker in _SQLITE_UNVERIFIABLE_MARKERS):
                # App-specific collation/extension (e.g. Plex icu_root) — the
                # stock sqlite3 binary cannot evaluate it, which is not corruption.
                integrity = "skipped"
                integrity_detail = "unverifiable"
            elif check_result.returncode == 0 and check_result.stdout.strip().lower() == "ok":
                integrity = "ok"
            else:
                detail = (check_result.stdout or check_result.stderr or "").strip()[:300]
                logger.warning("SQLite integrity check failed for %s/%s: %s", db.container_name, db.db_name, detail)
                return failed(
                    f"SQLite integrity check failed on the copy: {detail or 'no output'}", "failed", dump_path
                )

            size = os.path.getsize(partial) if os.path.exists(partial) else 0
            if size == 0:
                return failed("SQLite backup produced an empty file", "failed", dump_path)
            os.replace(partial, dump_path)
            return DumpResult(
                container_name=db.container_name,
                db_type="sqlite",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=size,
                integrity_check=integrity,
                status="success",
                integrity_detail=integrity_detail,
            )
        finally:
            self._discard(partial)

    def _dump_mariadb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MariaDB/MySQL via docker exec with streaming."""
        dump_path = self._dump_path(db, self._timestamp(), self._sql_ext())
        partial = self._partial_path(dump_path)

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
            stream = self._exec_stream_to_file(container, exec_cmd, partial, environment=exec_env)
            return self._streamed_dump_result(
                db, "mariadb", dump_binary, dump_path, stream, MYSQL_DUMP_TRAILER, partial
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
                error=redact_credentials(str(e)),
            )
        finally:
            self._discard(partial)

    def _dump_mongodb_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump MongoDB via docker exec with authentication support."""
        ext = ".archive.gz" if self.compression == "gzip" else ".archive"
        dump_path = self._dump_path(db, self._timestamp(), ext)
        partial = self._partial_path(dump_path)

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

            stream = self._exec_stream_to_file(container, exec_cmd, partial)
            return self._streamed_dump_result(db, "mongodb", "mongodump", dump_path, stream, None, partial)
        except Exception as e:
            return DumpResult(
                container_name=db.container_name,
                db_type="mongodb",
                db_name=db.db_name,
                dump_path=dump_path,
                dump_size_bytes=0,
                integrity_check="skipped",
                status="failed",
                error=redact_credentials(str(e)),
            )
        finally:
            self._discard(partial)

    def _redis_auth_env(self, container) -> dict[str, str]:
        """REDISCLI_AUTH for password-protected Redis (never passed in argv)."""
        password = self._get_container_env(container).get("REDIS_PASSWORD", "")
        if not password:
            try:
                cmd = container.attrs.get("Config", {}).get("Cmd") or []
                tokens: list[str] = []
                for item in cmd if isinstance(cmd, list) else [cmd]:
                    tokens.extend(shlex.split(item) if isinstance(item, str) else [])
                for index, token in enumerate(tokens[:-1]):
                    if token in ("--requirepass", "requirepass"):
                        password = tokens[index + 1]
                        break
            except (ValueError, AttributeError, TypeError):
                password = ""
        return {"REDISCLI_AUTH": password} if password else {}

    def _dump_redis_blocking(self, db: DiscoveredDatabase) -> DumpResult:
        """Dump Redis via BGSAVE (non-blocking) + LASTSAVE polling."""
        dump_path = self._dump_path(db, self._timestamp(), ".rdb")
        partial = self._partial_path(dump_path)

        def copied_result(source: str | None) -> DumpResult:
            """Copy ``source`` (None: already in ``partial``) and move it into place."""
            try:
                if source is not None:
                    shutil.copy2(source, partial)
                size = os.path.getsize(partial) if os.path.exists(partial) else 0
                if size > 0:
                    os.replace(partial, dump_path)
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
            return DumpResult(
                container_name=db.container_name,
                db_type="redis",
                db_name="redis",
                dump_path=dump_path,
                dump_size_bytes=size,
                integrity_check="skipped",
                status="success" if size > 0 else "failed",
                error=None if size > 0 else "Copied RDB file is empty",
            )

        try:
            container = self.docker.containers.get(db.container_name)
            auth_env = self._redis_auth_env(container)

            def redis_cli(*args: str):
                kwargs = {"environment": auth_env} if auth_env else {}
                return container.exec_run(["redis-cli", *args], **kwargs)

            # Get current LASTSAVE timestamp before triggering save
            exit_code, output = redis_cli("LASTSAVE")
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
            exit_code, output = redis_cli("BGSAVE")
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
                exit_code, output = redis_cli("LASTSAVE")
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
            exit_code, output = redis_cli("CONFIG", "GET", "dir")
            redis_dir = "/data"  # default
            if exit_code == 0:
                parts = (
                    output.decode(errors="replace").strip().split("\n")
                    if isinstance(output, bytes)
                    else str(output).strip().split("\n")
                )
                if len(parts) >= 2:
                    redis_dir = parts[1].strip()

            exit_code, output = redis_cli("CONFIG", "GET", "dbfilename")
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
                        return copied_result(host_rdb)

            # Fallback: scan all mount sources for the rdb filename
            for mount in mounts:
                source = mount.get("Source", "")
                rdb_path = os.path.join(source, rdb_filename)
                if os.path.exists(rdb_path):
                    return copied_result(rdb_path)

            copied, archive_error = self._copy_file_from_container_archive(container, rdb_container_path, partial)
            if copied:
                return copied_result(None)

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
        finally:
            self._discard(partial)

    def cleanup_old_dumps(
        self,
        keep_last: int = 1,
        max_age_days: int | None = 7,
        protect_prefixes: set[str] | None = None,
        legacy_prefixes: dict[str, str] | None = None,
    ) -> int:
        """Prune the dump directory. See ``cleanup_dump_dir``."""
        return cleanup_dump_dir(
            self.dump_dir,
            keep_last=keep_last,
            max_age_days=max_age_days,
            protect_prefixes=protect_prefixes,
            legacy_prefixes=legacy_prefixes,
        )

    async def dump_single(
        self,
        container_name: str,
        db_name: str,
        db_type: str,
        host_path: str | None = None,
        verify_integrity: bool = True,
    ) -> DumpResult:
        """Dump a single database on-demand (and record it in the dump manifest)."""
        db = DiscoveredDatabase(
            container_name=container_name,
            db_type=db_type,
            db_name=db_name,
            host_path=host_path,
        )
        self.compression = await self._load_compression()
        result = await self._dump_one(db)
        if result.status != "success":
            self._remove_dump_file(result.dump_path)
        await self._update_manifest([result], {id(result): host_path}, {container_name})
        return result
