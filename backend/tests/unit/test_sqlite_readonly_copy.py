"""SQLite copies from read-only mounts must not silently drop write-ahead-log data.

``immutable=1`` ignores the WAL: with an app actively writing, a copy made that
way can miss every recent change (verified against a real WAL database: the
copy lacked the table entirely) and still pass integrity_check.
"""

import os
import sqlite3
from types import SimpleNamespace

import pytest

from app.services.db_dumper import DBDumper

pytestmark = pytest.mark.asyncio


def _dumper(tmp_path):
    config = SimpleNamespace(dump_dir=tmp_path / "dumps", db_path=str(tmp_path / "arkive.db"))
    return DBDumper(docker_client=None, config=config)


def _result(code=0, err=""):
    return SimpleNamespace(returncode=code, stdout="", stderr=err)


class FakeBackup:
    """Records the sources tried; fails or succeeds per source kind and writes
    the copy (to ``partial``) when it succeeds, like sqlite3 .backup."""

    def __init__(self, partial, ro_ok: bool, on_immutable=None):
        self.partial = partial
        self.ro_ok = ro_ok
        self.on_immutable = on_immutable
        self.sources: list[str] = []

    def _ok(self):
        self.partial.write_bytes(b"SQLite format 3\x00")
        return _result(0)

    async def __call__(self, source: str):
        self.sources.append(source)
        if source.endswith("?mode=ro"):
            # sqlite3 exits 0 even when it can't open the file.
            return self._ok() if self.ro_ok else _result(0, "Error: unable to open database file")
        if "immutable=1" in source:
            if self.on_immutable:
                self.on_immutable()
            return self._ok()
        return _result(0, "Error: unable to open database file")


async def test_mode_ro_is_preferred_when_it_works(tmp_path):
    db = tmp_path / "app.db"
    db.write_bytes(b"x")
    backup = FakeBackup(tmp_path / "copy.partial", ro_ok=True)
    result, error = await _dumper(tmp_path)._sqlite_readonly_backup(str(db), str(tmp_path / "copy.partial"), backup)
    assert error is None and result.returncode == 0
    assert backup.sources == [f"file:{db}?mode=ro"]


async def test_uncheckpointed_wal_refuses_immutable_copy(tmp_path):
    db = tmp_path / "app.db"
    db.write_bytes(b"x")
    (tmp_path / "app.db-wal").write_bytes(b"pending changes")
    backup = FakeBackup(tmp_path / "copy.partial", ro_ok=False)
    result, error = await _dumper(tmp_path)._sqlite_readonly_backup(str(db), str(tmp_path / "copy.partial"), backup)
    assert result is None
    assert "uncheckpointed write-ahead-log" in error
    assert not any("immutable" in s for s in backup.sources)


async def test_idle_checkpointed_database_uses_immutable(tmp_path):
    db = tmp_path / "app.db"
    db.write_bytes(b"x")
    (tmp_path / "app.db-wal").write_bytes(b"")  # empty WAL: nothing to lose
    backup = FakeBackup(tmp_path / "copy.partial", ro_ok=False)
    result, error = await _dumper(tmp_path)._sqlite_readonly_backup(str(db), str(tmp_path / "copy.partial"), backup)
    assert error is None and result.returncode == 0
    assert backup.sources[-1] == f"file:{db}?mode=ro&immutable=1"


async def test_file_changing_during_immutable_copy_fails_after_retries(tmp_path):
    db = tmp_path / "app.db"
    db.write_bytes(b"x")
    counter = {"n": 0}

    def writer_touches_file():
        counter["n"] += 1
        db.write_bytes(b"x" * (counter["n"] + 1))

    backup = FakeBackup(tmp_path / "copy.partial", ro_ok=False, on_immutable=writer_touches_file)
    dumper = _dumper(tmp_path)
    result, error = await dumper._sqlite_readonly_backup(str(db), str(tmp_path / "copy.partial"), backup)
    assert result is None and "kept changing" in error
    assert counter["n"] == dumper.sqlite_immutable_attempts


async def test_wal_appearing_during_immutable_copy_is_retried(tmp_path):
    db = tmp_path / "app.db"
    db.write_bytes(b"x")
    calls = {"n": 0}

    def first_attempt_sees_a_writer():
        calls["n"] += 1
        wal = tmp_path / "app.db-wal"
        if calls["n"] == 1:
            wal.write_bytes(b"new write")
        elif wal.exists():
            wal.unlink()  # checkpointed and closed before the retry

    backup = FakeBackup(tmp_path / "copy.partial", ro_ok=False, on_immutable=first_attempt_sees_a_writer)
    result, error = await _dumper(tmp_path)._sqlite_readonly_backup(str(db), str(tmp_path / "copy.partial"), backup)
    assert error is None and calls["n"] == 2


async def test_real_wal_database_copied_with_mode_ro_keeps_wal_rows(tmp_path):
    """End to end with the real sqlite3 binary: rows still in the WAL survive."""
    import shutil

    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 binary not available")
    db = tmp_path / "app.db"
    writer = sqlite3.connect(db)
    writer.execute("pragma journal_mode=wal")
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("create table t(x)")
    writer.executemany("insert into t values (?)", [(i,) for i in range(500)])
    writer.commit()  # rows live in app.db-wal; the writer keeps -shm alive
    assert os.path.getsize(f"{db}-wal") > 0

    from app.utils.subprocess_runner import run_command

    out = tmp_path / "copy.db"

    async def run_backup(source):
        return await run_command(["sqlite3", source, f".backup {out}"])

    result, error = await _dumper(tmp_path)._sqlite_readonly_backup(str(db), str(out), run_backup)
    writer.close()
    assert error is None
    assert sqlite3.connect(out).execute("select count(*) from t").fetchone()[0] == 500
