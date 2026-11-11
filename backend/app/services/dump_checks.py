"""Integrity checks for MongoDB archives and Redis RDB files.

Both formats carry their own checksums, so a dump can be checked completely
before anything is changed, without a server:

- ``mongodump --archive``: a magic number, a prelude that lists every
  collection, then the documents of each collection, ending with an EOF
  header that carries a CRC-64 (ECMA) of all of that collection's documents.
  A dump that was cut short misses EOF headers; a damaged one fails a CRC.
  ``mongorestore --dryRun`` does not read the documents, so it is no check.
- Redis RDB: ``REDIS`` plus a 4-digit format version, then the data, an EOF
  opcode and (since RDB 5) a CRC-64 (Jones) of everything before it. A
  checksum of 0 means the file was written with ``rdbchecksum no``.

Checking reads the whole file once (CRC-64 in pure Python runs at about
10 MB/s); run it in a worker thread.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

MONGO_ARCHIVE_MAGIC = 0x8199E26D
# BSON documents are at most 16 MiB; archive headers are tiny. Anything far
# larger is a damaged length field, not a document.
MAX_BSON_BYTES = 48 * 1024 * 1024
_TERMINATOR = -1
MONGO_SYSTEM_DATABASES = frozenset({"admin", "config", "local"})

_RDB_HEADER_RE = re.compile(rb"^REDIS(\d{4})$")
RDB_OPCODE_EOF = 0xFF
RDB_OPCODE_AUX = 0xFA

# Newest RDB format each Redis release reads (src/rdb.h RDB_VERSION).
_RDB_VERSION_BY_REDIS = (
    ((7, 4), 12),
    ((7, 2), 11),
    ((7, 0), 10),
    ((5, 0), 9),
    ((4, 0), 8),
    ((3, 2), 7),
    ((2, 6), 6),
)
# Releases newer than this may read formats Arkive does not know about.
_NEWEST_KNOWN_REDIS = (8, 0)


class DumpCheckError(Exception):
    """The file is not a complete, undamaged dump; the message says why."""


def _crc64_table(poly: int) -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        table.append(crc)
    return table


_ECMA_TABLE = _crc64_table(0xC96C5795D7870F42)  # Go hash/crc64 ECMA (mongodump)
_JONES_TABLE = _crc64_table(0x95AC9329AC4BC9B5)  # Redis crc64.c
_MASK64 = 0xFFFFFFFFFFFFFFFF


def crc64_ecma(data: bytes, crc: int = 0) -> int:
    """CRC-64/ECMA as Go's hash/crc64 computes it (inverted in and out)."""
    table = _ECMA_TABLE
    crc ^= _MASK64
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ _MASK64


def crc64_jones(data: bytes, crc: int = 0) -> int:
    """CRC-64/Jones as Redis computes it for RDB files (no inversion)."""
    table = _JONES_TABLE
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc


# ---------------------------------------------------------------------------
# MongoDB archives
# ---------------------------------------------------------------------------


def _bson_fields(doc: bytes) -> dict:
    """Top-level scalar fields of a BSON document (archive headers only)."""
    out: dict = {}
    end = len(doc) - 1
    pos = 4
    try:
        while pos < end:
            kind = doc[pos]
            pos += 1
            name_end = doc.index(b"\x00", pos)
            name = doc[pos:name_end].decode("utf-8", errors="replace")
            pos = name_end + 1
            if kind == 0x02:  # string
                (length,) = struct.unpack_from("<i", doc, pos)
                out[name] = doc[pos + 4 : pos + 4 + length - 1].decode("utf-8", errors="replace")
                pos += 4 + length
            elif kind == 0x08:  # bool
                out[name] = doc[pos] == 1
                pos += 1
            elif kind == 0x10:  # int32
                (out[name],) = struct.unpack_from("<i", doc, pos)
                pos += 4
            elif kind in (0x12, 0x09, 0x11):  # int64, datetime, timestamp
                (out[name],) = struct.unpack_from("<q", doc, pos)
                pos += 8
            elif kind == 0x01:  # double
                (out[name],) = struct.unpack_from("<d", doc, pos)
                pos += 8
            elif kind in (0x03, 0x04):  # embedded document / array: skipped
                (length,) = struct.unpack_from("<i", doc, pos)
                pos += length
            elif kind == 0x05:  # binary
                (length,) = struct.unpack_from("<i", doc, pos)
                pos += 5 + length
            elif kind == 0x07:  # ObjectId
                pos += 12
            elif kind in (0x0A, 0x06, 0xFF, 0x7F):  # null, undefined, min/max key
                pass
            else:
                break  # nothing an archive header uses; stop reading fields
    except (ValueError, struct.error, IndexError):
        pass
    return out


@dataclass
class MongoCollection:
    db: str
    name: str
    type: str = "collection"  # collection | view | timeseries
    documents: int = 0

    @property
    def data_namespace(self) -> tuple[str, str] | None:
        """The namespace that holds this collection's documents in the archive body."""
        if self.type == "view":
            return None
        if self.type == "timeseries":
            return (self.db, f"system.buckets.{self.name}")
        return (self.db, self.name)


@dataclass
class MongoArchive:
    server_version: str | None = None
    tool_version: str | None = None
    size_bytes: int = 0
    collections: list[MongoCollection] = field(default_factory=list)

    @property
    def databases(self) -> set[str]:
        return {c.db for c in self.collections}

    def user_databases(self) -> set[str]:
        return self.databases - MONGO_SYSTEM_DATABASES

    def counted(self, databases) -> list[MongoCollection]:
        """Ordinary collections in ``databases`` whose documents can be counted after a restore."""
        wanted = set(databases)
        return [
            c
            for c in self.collections
            if c.db in wanted and c.type == "collection" and not c.name.startswith("system.")
        ]


class _Reader:
    def __init__(self, fh, size: int):
        self.fh = fh
        self.size = size
        self.pos = 0

    def int32(self, what: str) -> int | None:
        """The next little-endian int32, or None at a clean end of file."""
        raw = self.fh.read(4)
        if not raw:
            return None
        if len(raw) < 4:
            raise DumpCheckError(f"the archive ends in the middle of {what} (truncated at byte {self.pos})")
        self.pos += 4
        return struct.unpack("<i", raw)[0]

    def document(self, length: int, what: str) -> bytes:
        if length < 5 or length > MAX_BSON_BYTES:
            raise DumpCheckError(f"{what}: invalid length {length} at byte {self.pos - 4} (damaged archive)")
        rest = self.fh.read(length - 4)
        if len(rest) < length - 4:
            raise DumpCheckError(f"the archive ends in the middle of {what} (truncated at byte {self.pos})")
        self.pos += length - 4
        doc = struct.pack("<i", length) + rest
        if doc[-1] != 0:
            raise DumpCheckError(f"damaged {what} at byte {self.pos - length}")
        return doc


def read_mongo_archive(path: Path | str) -> MongoArchive:
    """Check a ``mongodump --archive`` file from end to end; raises DumpCheckError."""
    path = Path(path)
    size = path.stat().st_size
    archive = MongoArchive(size_bytes=size)
    with open(path, "rb", buffering=1024 * 1024) as fh:
        reader = _Reader(fh, size)
        magic = reader.int32("the header")
        if magic is None or (magic & 0xFFFFFFFF) != MONGO_ARCHIVE_MAGIC:
            raise DumpCheckError("not a mongodump archive (wrong magic number)")

        length = reader.int32("the header")
        if length is None:
            raise DumpCheckError("the archive ends after its magic number (truncated)")
        header = _bson_fields(reader.document(length, "the archive header"))
        if "version" not in header:
            raise DumpCheckError("the archive header has no format version (damaged archive)")
        archive.server_version = header.get("server_version") or None
        archive.tool_version = header.get("tool_version") or None

        seen: set[tuple[str, str]] = set()
        while True:
            length = reader.int32("the collection list")
            if length is None:
                raise DumpCheckError("the archive ends inside its collection list (truncated)")
            if length == _TERMINATOR:
                break
            meta = _bson_fields(reader.document(length, "collection metadata"))
            db, name = meta.get("db"), meta.get("collection")
            if not isinstance(db, str) or not isinstance(name, str):
                raise DumpCheckError("collection metadata without a database or collection name (damaged archive)")
            kind = meta.get("type")
            if not isinstance(kind, str) or not kind:
                try:
                    kind = json.loads(meta.get("metadata") or "{}").get("type") or "collection"
                except (ValueError, AttributeError):
                    kind = "collection"
            if (db, name) in seen:
                raise DumpCheckError(f"{db}.{name} is listed twice (damaged archive)")
            seen.add((db, name))
            archive.collections.append(MongoCollection(db=db, name=name, type=kind))

        crcs: dict[tuple[str, str], int] = {}
        counts: dict[tuple[str, str], int] = {}
        closed: set[tuple[str, str]] = set()
        while True:
            length = reader.int32("a collection header")
            if length is None:
                break
            if length == _TERMINATOR:
                raise DumpCheckError(f"unexpected block terminator at byte {reader.pos - 4} (damaged archive)")
            ns_header = _bson_fields(reader.document(length, "a collection header"))
            db, name = ns_header.get("db"), ns_header.get("collection")
            if not isinstance(db, str) or not isinstance(name, str):
                raise DumpCheckError(f"collection header without a namespace at byte {reader.pos} (damaged archive)")
            ns = (db, name)
            if ns in closed:
                raise DumpCheckError(f"{db}.{name} continues after its end marker (damaged archive)")
            eof = bool(ns_header.get("EOF"))
            crc = crcs.get(ns, 0)
            docs = 0
            while True:
                length = reader.int32(f"the documents of {db}.{name}")
                if length is None:
                    raise DumpCheckError(f"the archive ends inside {db}.{name} (truncated)")
                if length == _TERMINATOR:
                    break
                if eof:
                    raise DumpCheckError(f"documents after the end marker of {db}.{name} (damaged archive)")
                doc = reader.document(length, f"a document of {db}.{name}")
                crc = crc64_ecma(doc, crc)
                docs += 1
            crcs[ns] = crc
            counts[ns] = counts.get(ns, 0) + docs
            if eof:
                expected = ns_header.get("CRC")
                if not isinstance(expected, int) or (expected & _MASK64) != crc:
                    raise DumpCheckError(f"checksum mismatch in {db}.{name}: the archive is damaged")
                closed.add(ns)

    for collection in archive.collections:
        ns = collection.data_namespace
        if ns is None:
            continue
        if ns not in closed:
            raise DumpCheckError(
                f"{collection.db}.{collection.name} has no end marker: the archive was cut short "
                "(the dump did not finish)"
            )
        collection.documents = counts.get(ns, 0)
    open_namespaces = sorted(f"{db}.{name}" for db, name in set(counts) - closed)
    if open_namespaces:
        raise DumpCheckError(f"{', '.join(open_namespaces[:3])} has no end marker: the archive was cut short")
    return archive


# ---------------------------------------------------------------------------
# Redis RDB files
# ---------------------------------------------------------------------------


@dataclass
class RdbInfo:
    version: int
    size_bytes: int
    checksum_verified: bool
    redis_version: str | None = None  # the "redis-ver" aux field: the Redis that wrote it
    created: int | None = None  # the "ctime" aux field (unix time)


def _rdb_length(data: bytes, pos: int) -> tuple[int, int, bool]:
    """(value, new position, is an encoded integer/LZF marker) of an RDB length field."""
    first = data[pos]
    kind = first >> 6
    if kind == 0:
        return first & 0x3F, pos + 1, False
    if kind == 1:
        return ((first & 0x3F) << 8) | data[pos + 1], pos + 2, False
    if kind == 2:
        if first == 0x80:
            return struct.unpack_from(">I", data, pos + 1)[0], pos + 5, False
        if first == 0x81:
            return struct.unpack_from(">Q", data, pos + 1)[0], pos + 9, False
        raise ValueError("unknown length encoding")
    return first & 0x3F, pos + 1, True


def _rdb_string(data: bytes, pos: int) -> tuple[str, int]:
    value, pos, special = _rdb_length(data, pos)
    if not special:
        return data[pos : pos + value].decode("utf-8", errors="replace"), pos + value
    if value == 0:
        return str(struct.unpack_from("<b", data, pos)[0]), pos + 1
    if value == 1:
        return str(struct.unpack_from("<h", data, pos)[0]), pos + 2
    if value == 2:
        return str(struct.unpack_from("<i", data, pos)[0]), pos + 4
    raise ValueError("compressed string")


def _rdb_aux(head: bytes) -> dict[str, str]:
    """Aux fields (redis-ver, ctime, ...) at the start of an RDB file; best effort."""
    fields: dict[str, str] = {}
    pos = 9
    try:
        while pos < len(head) and head[pos] == RDB_OPCODE_AUX:
            key, pos = _rdb_string(head, pos + 1)
            value, pos = _rdb_string(head, pos)
            fields[key] = value
    except (ValueError, struct.error, IndexError):
        pass
    return fields


def read_rdb(path: Path | str) -> RdbInfo:
    """Check a Redis RDB file: header, EOF opcode and checksum; raises DumpCheckError."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb", buffering=1024 * 1024) as fh:
        head = fh.read(4096)
        match = _RDB_HEADER_RE.match(head[:9])
        if not match:
            raise DumpCheckError("not a Redis RDB file (no REDIS header)")
        version = int(match.group(1))
        if version < 1:
            raise DumpCheckError(f"invalid RDB format version {version}")
        has_checksum = version >= 5
        trailer = 9 if has_checksum else 1
        if size < 9 + trailer:
            raise DumpCheckError("the RDB file is truncated (no end marker)")
        fh.seek(size - trailer)
        tail = fh.read(trailer)
        if tail[0] != RDB_OPCODE_EOF:
            raise DumpCheckError("the RDB file is truncated or damaged (no end marker)")
        verified = False
        if has_checksum:
            (expected,) = struct.unpack("<Q", tail[1:])
            if expected:
                fh.seek(0)
                crc = 0
                remaining = size - 8
                while remaining > 0:
                    chunk = fh.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise DumpCheckError("the RDB file changed while it was checked")
                    crc = crc64_jones(chunk, crc)
                    remaining -= len(chunk)
                if crc != expected:
                    raise DumpCheckError("checksum mismatch: the RDB file is damaged")
                verified = True
    aux = _rdb_aux(head)
    created = aux.get("ctime")
    return RdbInfo(
        version=version,
        size_bytes=size,
        checksum_verified=verified,
        redis_version=aux.get("redis-ver"),
        created=int(created) if created and created.lstrip("-").isdigit() else None,
    )


def parse_version(text: str | None) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def redis_reads_rdb(redis_version: str | None, rdb_version: int) -> bool | None:
    """Whether a Redis release reads RDB format ``rdb_version``; None when Arkive cannot tell."""
    version = parse_version(redis_version)
    if version is None:
        return None
    for release, newest in _RDB_VERSION_BY_REDIS:
        if version[:2] >= release:
            if rdb_version <= newest:
                return True
            # A release newer than the table may read newer formats.
            return None if version[:2] > _NEWEST_KNOWN_REDIS else False
    return rdb_version <= 6
