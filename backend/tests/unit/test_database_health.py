"""Per-database health: dump history, size trend, shrink detection, drills and
"restorable as of" (services/database_health.py)."""

import json

import aiosqlite
import pytest
import pytest_asyncio

from app.services.database_health import (
    MIB,
    ShrinkPolicy,
    accept_current_size,
    database_health,
    detect_shrink,
    dump_file_key,
    dump_file_time,
    dump_format,
    result_key,
    shrink_baseline,
    shrink_warnings,
)
from app.services.db_dumper import DumpResult, dump_prefix

pytestmark = pytest.mark.asyncio

PG = {"container_name": "app-db", "db_type": "postgres", "db_name": "app", "host_path": None}
PG_KEY = dump_prefix("app-db", "postgres", "app")


@pytest_asyncio.fixture
async def db(tmp_path):
    from app.core.database import init_db, run_migrations

    path = tmp_path / "arkive.db"
    await init_db(path)
    await run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("INSERT INTO backup_jobs (id, name, schedule) VALUES ('job', 'Job', '0 2 * * *')")
        await conn.execute("INSERT INTO storage_targets (id, name, type) VALUES ('t1', 'Local', 'local')")
        await conn.commit()
        yield conn


async def discover(db, *entries):
    by_container: dict[str, list] = {}
    for entry in entries:
        by_container.setdefault(entry["container_name"], []).append(entry)
    for name, dbs in by_container.items():
        await db.execute(
            "INSERT OR REPLACE INTO discovered_containers (name, image, status, databases) VALUES (?, 'img', 'running', ?)",
            (name, json.dumps(dbs)),
        )
    await db.commit()


def dump_name(entry, day: int, ext: str = ".sql", hour: int = 2) -> str:
    prefix = dump_prefix(entry["container_name"], entry["db_type"], entry["db_name"], entry.get("host_path"))
    return f"/config/dumps/{prefix}_202611{day:02d}_{hour:02d}0000{ext}"


async def add_run(
    db,
    run_id: str,
    day: int,
    dumps: list[tuple[dict, str, int]],
    *,
    uploaded: bool = True,
    ext: str = ".sql",
    keyed: bool = True,
    run_status: str = "success",
):
    """A run on 2026-11-<day> 02:00 dumping (entry, status, size) and uploading."""
    started = f"2026-11-{day:02d}T02:00:00Z"
    await db.execute(
        "INSERT INTO job_runs (id, job_id, status, started_at, completed_at) VALUES (?, 'job', ?, ?, ?)",
        (run_id, run_status, started, None if run_status == "running" else f"2026-11-{day:02d}T02:30:00Z"),
    )
    for entry, status, size in dumps:
        path = dump_name(entry, day, ext) if status != "skipped" else ""
        key = dump_prefix(entry["container_name"], entry["db_type"], entry["db_name"], entry.get("host_path"))
        await db.execute(
            """INSERT INTO job_run_databases
               (run_id, container_name, db_type, db_name, dump_size_bytes, integrity_check, status,
                host_path, error, db_key, dumped_at)
               VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?, ?)""",
            (
                run_id,
                entry["container_name"],
                entry["db_type"],
                entry["db_name"],
                size,
                status,
                path,
                "pg_dump exited with code 1" if status == "failed" else None,
                key if keyed else None,
                f"2026-11-{day:02d}T02:05:00Z" if keyed else None,
            ),
        )
    if uploaded is not None:
        await db.execute(
            "INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id) VALUES (?, 't1', ?, ?)",
            (run_id, "success" if uploaded else "failed", f"snap{day}" if uploaded else ""),
        )
    await db.commit()


def result(entry, size: int, ext: str = ".sql", day: int = 20) -> DumpResult:
    return DumpResult(
        container_name=entry["container_name"],
        db_type=entry["db_type"],
        db_name=entry["db_name"],
        dump_path=dump_name(entry, day, ext),
        dump_size_bytes=size,
        integrity_check="ok",
        status="success",
        db_key=dump_prefix(entry["container_name"], entry["db_type"], entry["db_name"], entry.get("host_path")),
    )


# --- pure helpers ---------------------------------------------------------------------------


async def test_dump_file_helpers():
    name = dump_name(PG, 5, ".sql.gz")
    assert dump_file_key(name) == PG_KEY
    assert dump_file_time(name) == "2026-11-05T02:00:00Z"
    assert dump_format(name) == ".sql.gz"
    # Names from before the identity hash carry no key.
    assert dump_file_key("/config/dumps/app-db_app_20261105_020000.sql") is None
    assert dump_file_time("/config/dumps/app-db_app_20261105_020000.sql") == "2026-11-05T02:00:00Z"
    assert dump_format("/x") == ""


async def test_result_key_prefers_recorded_identity():
    sqlite_entry = {"container_name": "app", "db_type": "sqlite", "db_name": "app.db", "host_path": "/mnt/a/app.db"}
    dr = result(sqlite_entry, 10)
    assert result_key(dr) == dump_prefix("app", "sqlite", "app.db", "/mnt/a/app.db")
    dr.db_key = None  # still recoverable from the file name
    assert result_key(dr) == dump_prefix("app", "sqlite", "app.db", "/mnt/a/app.db")
    dr.dump_path = ""
    assert result_key(dr) == dump_prefix("app", "sqlite", "app.db")


async def test_shrink_needs_enough_history_and_a_meaningful_size():
    policy = ShrinkPolicy(percent=50, min_bytes=MIB)
    assert detect_shrink(1, [100 * MIB, 100 * MIB], policy) is None  # two samples only
    assert detect_shrink(1, [512 * 1024] * 5, policy) is None  # below the size floor
    assert detect_shrink(1, [100 * MIB] * 5, ShrinkPolicy(percent=0)) is None  # disabled


async def test_shrink_threshold_is_inclusive_and_uses_the_median():
    policy = ShrinkPolicy(percent=50, min_bytes=MIB)
    history = [100 * MIB] * 5
    assert detect_shrink(51 * MIB, history, policy) is None
    check = detect_shrink(50 * MIB, history, policy)
    assert check is not None and check.drop_percent == 50 and check.baseline_bytes == 100 * MIB
    # One odd dump in the history does not move the baseline.
    assert shrink_baseline([10 * MIB, 100 * MIB, 100 * MIB, 100 * MIB, 100 * MIB]) == 100 * MIB
    # Only the newest five count.
    assert shrink_baseline([10 * MIB] * 5 + [100 * MIB] * 20) == 10 * MIB


# --- run-time shrink warnings ---------------------------------------------------------------


async def test_sharp_shrink_produces_a_warning(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)])

    warnings = await shrink_warnings(db, "now", [result(PG, 40 * MIB)])

    assert len(warnings) == 1
    assert warnings[0].startswith("app-db/app: dump is 80% smaller than its recent median")
    assert "40.0 MB vs 200.0 MB" in warnings[0]


async def test_normal_growth_and_small_dips_do_not_warn(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", (200 + day) * MIB)])
    assert await shrink_warnings(db, "now", [result(PG, 180 * MIB)]) == []


async def test_threshold_is_configurable(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)])
    await db.execute("INSERT INTO settings (key, value) VALUES ('dump_shrink_alert_percent', '20')")
    await db.commit()
    assert len(await shrink_warnings(db, "now", [result(PG, 150 * MIB)])) == 1
    await db.execute("UPDATE settings SET value = '0' WHERE key = 'dump_shrink_alert_percent'")
    await db.commit()
    assert await shrink_warnings(db, "now", [result(PG, 1 * MIB)]) == []


async def test_compression_change_is_not_a_shrink(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)])
    # Switching dump_compression to gzip: much smaller, different format.
    assert await shrink_warnings(db, "now", [result(PG, 20 * MIB, ext=".sql.gz")]) == []


async def test_failed_and_current_run_rows_are_not_history(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "failed", 0)])
    await add_run(db, "now", 6, [(PG, "success", 1 * MIB)])
    assert await shrink_warnings(db, "now", [result(PG, 1 * MIB, day=6)]) == []


async def test_same_named_sqlite_files_have_separate_histories(db):
    big = {"container_name": "app", "db_type": "sqlite", "db_name": "app.db", "host_path": "/mnt/a/app.db"}
    small = {"container_name": "app", "db_type": "sqlite", "db_name": "app.db", "host_path": "/mnt/b/app.db"}
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(big, "success", 500 * MIB), (small, "success", 5 * MIB)])
    assert await shrink_warnings(db, "now", [result(big, 490 * MIB), result(small, 5 * MIB)]) == []


async def test_legacy_rows_without_identity_are_matched_by_name(db):
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)], keyed=False)
    await db.execute("UPDATE job_run_databases SET host_path = '/config/dumps/app-db_app_20261101_020000.sql'")
    await db.commit()
    assert len(await shrink_warnings(db, "now", [result(PG, 10 * MIB)])) == 1


async def test_accepting_the_new_size_resets_the_baseline(db):
    await discover(db, PG)
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)])
    await add_run(db, "r6", 6, [(PG, "success", 40 * MIB)])
    assert (await database_health(db))["items"][0]["shrunk"] is True

    accepted = await accept_current_size(db, PG_KEY)

    assert accepted["accepted_from"] == "2026-11-06T02:05:00Z"
    assert accepted["db_name"] == "app"
    assert (await database_health(db))["items"][0]["shrunk"] is False
    assert await shrink_warnings(db, "now", [result(PG, 40 * MIB)]) == []
    assert await accept_current_size(db, "unknown-key") is None


# --- per-database health --------------------------------------------------------------------


async def test_health_reports_last_dump_trend_and_restorable_point(db):
    await discover(db, PG)
    for day in range(1, 4):
        await add_run(db, f"r{day}", day, [(PG, "success", (100 + day) * MIB)])

    body = await database_health(db)

    assert body["total"] == 1 and body["summary"]["ok"] == 1
    item = body["items"][0]
    assert item["key"] == PG_KEY
    assert item["status"] == "ok" and item["issues"] == []
    assert item["last_success_at"] == "2026-11-03T02:05:00Z"
    assert item["size_bytes"] == 103 * MIB
    assert item["size_change_percent"] == pytest.approx(1.0, abs=0.1)
    assert [p["size_bytes"] for p in item["trend"]] == [101 * MIB, 102 * MIB, 103 * MIB]  # oldest first
    assert item["restorable_as_of"] == "2026-11-03T02:05:00Z"
    assert body["policy"]["shrink_alert_percent"] == 50


async def test_restorable_as_of_ignores_dumps_no_snapshot_holds(db):
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 100 * MIB)])
    await add_run(db, "r2", 2, [(PG, "success", 100 * MIB)], uploaded=False)

    item = (await database_health(db))["items"][0]

    assert item["last_success_at"] == "2026-11-02T02:05:00Z"
    assert item["restorable_as_of"] == "2026-11-01T02:05:00Z"
    assert item["status"] == "warning"
    assert "The newest dump is not in a snapshot yet" in item["issues"]


async def test_a_later_upload_covers_an_earlier_dump(db):
    """A flash-only run uploads the dump directory, so it covers the dump
    whose own run failed to upload."""
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 100 * MIB)], uploaded=False)
    await db.execute(
        "INSERT INTO job_runs (id, job_id, status, started_at, completed_at) "
        "VALUES ('flash', 'job', 'success', '2026-11-01T05:00:00Z', '2026-11-01T05:10:00Z')"
    )
    await db.execute(
        "INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id) VALUES ('flash', 't1', 'success', 'x')"
    )
    await db.commit()

    item = (await database_health(db))["items"][0]

    assert item["restorable_as_of"] == "2026-11-01T02:05:00Z"
    assert item["status"] == "ok"


async def test_uploads_to_deleted_targets_do_not_count(db):
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 100 * MIB)])
    await db.execute("DELETE FROM storage_targets")
    await db.commit()

    item = (await database_health(db))["items"][0]

    assert item["restorable_as_of"] is None
    assert "No snapshot holds a successful dump yet" in item["issues"]


async def test_running_run_is_not_reported_as_not_uploaded(db):
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 100 * MIB)])
    await add_run(db, "r2", 2, [(PG, "success", 100 * MIB)], uploaded=None, run_status="running")

    item = (await database_health(db))["items"][0]

    assert item["status"] == "ok"
    assert item["restorable_as_of"] == "2026-11-01T02:05:00Z"


async def test_failed_never_dumped_excluded_and_skipped(db):
    failing = {"container_name": "a", "db_type": "postgres", "db_name": "broken", "host_path": None}
    new = {"container_name": "b", "db_type": "mariadb", "db_name": "fresh", "host_path": None}
    off = {"container_name": "c", "db_type": "redis", "db_name": "cache", "host_path": None}
    stopped = {"container_name": "d", "db_type": "postgres", "db_name": "idle", "host_path": None}
    await discover(db, failing, new, off, stopped)
    await add_run(db, "r1", 1, [(failing, "success", 10 * MIB), (stopped, "success", 10 * MIB)])
    await add_run(db, "r2", 2, [(failing, "failed", 0), (stopped, "skipped", 0)])
    await db.execute(
        "INSERT INTO settings (key, value) VALUES ('excluded_databases', ?)",
        (json.dumps([{**off}]),),
    )
    await db.commit()

    body = await database_health(db)
    items = {i["db_name"]: i for i in body["items"]}

    assert items["broken"]["status"] == "failed"
    assert items["broken"]["issues"][0].startswith("Last dump failed: pg_dump exited")
    assert items["broken"]["last_success_at"] == "2026-11-01T02:05:00Z"
    assert [p["status"] for p in items["broken"]["trend"]] == ["success", "failed"]
    assert items["fresh"]["status"] == "never_dumped"
    assert items["cache"]["status"] == "excluded" and items["cache"]["issues"] == []
    assert items["idle"]["status"] == "ok"
    assert items["idle"]["notes"] and items["idle"]["restorable_as_of"] == "2026-11-01T02:05:00Z"
    assert body["summary"] == {"ok": 1, "warning": 0, "failed": 1, "never_dumped": 1, "excluded": 1}


async def test_shrunk_latest_dump_is_a_warning_and_marked_in_the_trend(db):
    await discover(db, PG)
    for day in range(1, 6):
        await add_run(db, f"r{day}", day, [(PG, "success", 200 * MIB)])
    await add_run(db, "r6", 6, [(PG, "success", 30 * MIB)])

    item = (await database_health(db))["items"][0]

    assert item["status"] == "warning" and item["shrunk"] is True
    assert item["shrink_percent"] == 85 and item["baseline_bytes"] == 200 * MIB
    assert item["trend"][-1]["shrunk"] is True
    assert not any(p["shrunk"] for p in item["trend"][:-1])


async def test_last_drill_result_is_matched_by_dump_identity(db):
    other = {"container_name": "app-db", "db_type": "postgres", "db_name": "other", "host_path": None}
    await discover(db, PG, other)
    await add_run(db, "r1", 1, [(PG, "success", 10 * MIB), (other, "success", 10 * MIB)])
    for drill_id, day, status in (("d1", 2, "failed"), ("d2", 3, "passed")):
        await db.execute(
            "INSERT INTO restore_drills (id, status, started_at, completed_at, snapshot_id) VALUES (?, ?, ?, ?, 'snap1')",
            (drill_id, status, f"2026-11-{day:02d}T07:00:00Z", f"2026-11-{day:02d}T07:05:00Z"),
        )
        await db.execute(
            """INSERT INTO restore_drill_results (drill_id, container_name, db_type, db_name, dump_file, status, detail)
               VALUES (?, 'app-db', 'postgres', 'app', ?, ?, 'loaded 12 tables')""",
            (drill_id, dump_name(PG, 1).rsplit("/", 1)[-1], status),
        )
    # A newer skip doesn't hide the last real result.
    await db.execute(
        "INSERT INTO restore_drills (id, status, started_at) VALUES ('d3', 'skipped', '2026-11-04T07:00:00Z')"
    )
    await db.execute(
        """INSERT INTO restore_drill_results (drill_id, container_name, db_type, db_name, dump_file, status, detail)
           VALUES ('d3', 'app-db', 'postgres', 'app', ?, 'skipped', 'over the size cap')""",
        (dump_name(PG, 1).rsplit("/", 1)[-1],),
    )
    await db.commit()

    items = {i["db_name"]: i for i in (await database_health(db))["items"]}

    drill = items["app"]["last_drill"]
    assert drill["status"] == "passed" and drill["drill_id"] == "d2"
    assert drill["at"] == "2026-11-03T07:05:00Z"
    assert drill["dump_dumped_at"] == "2026-11-01T02:00:00Z"
    assert items["other"]["last_drill"] is None


async def test_failed_drill_makes_database_a_warning(db):
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 10 * MIB)])
    await db.execute(
        "INSERT INTO restore_drills (id, status, started_at) VALUES ('d1', 'failed', '2026-11-02T07:00:00Z')"
    )
    await db.execute(
        """INSERT INTO restore_drill_results (drill_id, container_name, db_type, db_name, dump_file, status)
           VALUES ('d1', 'app-db', 'postgres', 'app', 'app-db_app_20261101_020000.sql', 'failed')"""
    )
    await db.commit()

    item = (await database_health(db))["items"][0]

    assert item["last_drill"]["status"] == "failed"  # legacy file name: matched by name
    assert item["status"] == "warning"
    assert "Last restore drill failed" in item["issues"]


# Keep in sync with frontend/src/lib/api/databases.test.ts (the demo-mode mock).
HEALTH_ITEM_FIELDS = {
    "key",
    "container_name",
    "db_type",
    "db_name",
    "host_path",
    "excluded",
    "status",
    "issues",
    "notes",
    "last_attempt",
    "last_success_at",
    "last_success_run_id",
    "size_bytes",
    "integrity_check",
    "baseline_bytes",
    "size_change_percent",
    "shrunk",
    "shrink_percent",
    "trend",
    "last_drill",
    "restorable_as_of",
}


async def test_response_fields_match_the_frontend_contract(db):
    await discover(db, PG)
    await add_run(db, "r1", 1, [(PG, "success", 10 * MIB)])
    body = await database_health(db)
    assert set(body) == {"items", "databases", "total", "summary", "policy", "latest_upload_at", "generated_at"}
    assert set(body["items"][0]) == HEALTH_ITEM_FIELDS
    assert set(body["items"][0]["trend"][0]) == {"run_id", "at", "status", "size_bytes", "shrunk"}


async def test_no_discovered_databases(db):
    body = await database_health(db)
    assert body["items"] == [] and body["total"] == 0


async def test_migration_7_adds_identity_and_dump_time(db):
    cursor = await db.execute("PRAGMA table_info(job_run_databases)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert {"db_key", "dumped_at"} <= columns
