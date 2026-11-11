"""MongoDB archive and Redis RDB integrity checks (services/dump_checks.py)."""

import struct

import pytest

from app.services.dump_checks import (
    DumpCheckError,
    crc64_ecma,
    crc64_jones,
    parse_version,
    read_mongo_archive,
    read_rdb,
    redis_reads_rdb,
)
from tests.fakes.fake_mongo_redis import bson, mongo_archive, rdb_file

DATA = {
    "app": {"notes": [{"_id": i, "text": "x" * 50} for i in range(20)], "empty": []},
    "admin": {"system.users": [{"_id": "admin.root", "user": "root"}]},
}


def test_crc64_check_values():
    # Go hash/crc64 ECMA (what mongodump writes) and Redis's crc64 (its own test vector).
    assert crc64_ecma(b"123456789") == 0x995DC9BBDF1939FA
    assert crc64_jones(b"123456789") == 0xE9C6D914C4B8D9CA
    # Incremental updates give the same result.
    assert crc64_ecma(b"6789", crc64_ecma(b"12345")) == crc64_ecma(b"123456789")
    assert crc64_jones(b"6789", crc64_jones(b"12345")) == crc64_jones(b"123456789")


def test_complete_archive_is_read(tmp_path):
    path = tmp_path / "a.archive"
    path.write_bytes(mongo_archive(DATA, views={"app": ["recent"]}))

    archive = read_mongo_archive(path)

    assert archive.server_version == "8.0.4" and archive.tool_version == "100.10.0"
    assert archive.databases == {"app", "admin"} and archive.user_databases() == {"app"}
    counted = {c.name: c.documents for c in archive.counted(["app"])}
    assert counted == {"notes": 20, "empty": 0}  # the view has no documents to count
    assert {c.name: c.type for c in archive.collections if c.db == "app"}["recent"] == "view"


def test_every_truncation_point_is_detected(tmp_path):
    data = mongo_archive(DATA)
    path = tmp_path / "t.archive"
    for cut in range(0, len(data) - 1, 7):
        path.write_bytes(data[:cut])
        with pytest.raises(DumpCheckError):
            read_mongo_archive(path)


def test_flipped_byte_in_a_document_fails_the_checksum(tmp_path):
    data = bytearray(mongo_archive(DATA))
    marker = data.index(b"x" * 50)
    data[marker + 10] ^= 0x01
    path = tmp_path / "f.archive"
    path.write_bytes(bytes(data))
    with pytest.raises(DumpCheckError, match="checksum mismatch in app.notes"):
        read_mongo_archive(path)


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "wrong magic"),
        (b"junkjunkjunk", "wrong magic"),
        (struct.pack("<I", 0x8199E26D) + struct.pack("<i", 2**30), "the archive header: invalid length"),
    ],
)
def test_not_an_archive(tmp_path, data, message):
    path = tmp_path / "x.archive"
    path.write_bytes(data)
    with pytest.raises(DumpCheckError, match=message):
        read_mongo_archive(path)


def test_documents_after_the_end_marker_are_refused(tmp_path):
    good = mongo_archive({"app": {"c": [{"_id": 1}]}})
    eof_header = bson({"db": "app", "collection": "c", "EOF": True, "CRC": 0}, int64_keys=("CRC",))
    tampered = good + eof_header + bson({"_id": 2}) + struct.pack("<i", -1)
    path = tmp_path / "x.archive"
    path.write_bytes(tampered)
    with pytest.raises(DumpCheckError, match="continues after its end marker"):
        read_mongo_archive(path)


def test_empty_archive_of_a_missing_database(tmp_path):
    path = tmp_path / "e.archive"
    path.write_bytes(mongo_archive({}))
    archive = read_mongo_archive(path)
    assert archive.collections == [] and archive.databases == set()


def test_rdb_with_checksum(tmp_path):
    path = tmp_path / "dump.rdb"
    path.write_bytes(rdb_file(version=12, redis_ver="7.4.1"))
    info = read_rdb(path)
    assert (info.version, info.redis_version, info.checksum_verified) == (12, "7.4.1", True)
    assert info.created == 1793534400


def test_rdb_without_checksum_is_read_but_not_verified(tmp_path):
    path = tmp_path / "dump.rdb"
    path.write_bytes(rdb_file(checksum=False))
    assert read_rdb(path).checksum_verified is False


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d[:-1], "truncated or damaged"),
        (lambda d: d[:5], "no REDIS header"),
        (lambda d: d[:12] + bytes([d[12] ^ 0x40]) + d[13:], "checksum mismatch"),
        (lambda d: b"REDIS0011\xff", "truncated"),
    ],
)
def test_damaged_rdb(tmp_path, mutate, message):
    path = tmp_path / "dump.rdb"
    path.write_bytes(mutate(rdb_file()))
    with pytest.raises(DumpCheckError, match=message):
        read_rdb(path)


def test_redis_reads_rdb():
    assert parse_version("7.2.4") == (7, 2, 4)
    assert redis_reads_rdb("6.2.14", 9) is True
    assert redis_reads_rdb("6.2.14", 10) is False
    assert redis_reads_rdb("7.0.15", 11) is False
    assert redis_reads_rdb("7.2.4", 11) is True
    assert redis_reads_rdb("7.4.1", 12) is True
    assert redis_reads_rdb("8.0.2", 12) is True
    assert redis_reads_rdb("8.0.2", 13) is False
    assert redis_reads_rdb("8.4.0", 13) is None  # newer than Arkive knows
    assert redis_reads_rdb(None, 9) is None
