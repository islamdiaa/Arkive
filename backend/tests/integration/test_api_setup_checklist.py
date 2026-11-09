"""Integration tests for GET /api/setup-checklist and POST /api/setup-checklist/dismiss.

Every step is judged from evidence in the database; these tests seed that
evidence directly and check what each step reports.
"""

import json

import aiosqlite
import pytest

from app.api.setup_checklist import STEP_IDS
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio

URL = "/api/setup-checklist"


def _db_path(client):
    return client._transport.app.state.config.db_path


async def _exec(client, sql: str, params: tuple | dict = ()) -> None:
    async with aiosqlite.connect(_db_path(client)) as db:
        await db.execute(sql, params)
        await db.commit()


async def _job_id(client, job_type: str) -> str:
    async with aiosqlite.connect(_db_path(client)) as db:
        cursor = await db.execute("SELECT id FROM backup_jobs WHERE type = ? LIMIT 1", (job_type,))
        return (await cursor.fetchone())[0]


async def _add_run(
    client,
    run_id: str,
    job_type: str,
    status: str,
    started_at: str,
    *,
    target_status: str | None = None,
    databases_discovered: int = 0,
    databases_dumped: int = 0,
    error_message: str | None = None,
) -> None:
    job_id = await _job_id(client, job_type)
    completed_at = None if status == "running" else started_at.replace("T03:00", "T03:05")
    await _exec(
        client,
        """INSERT INTO job_runs (id, job_id, status, trigger, started_at, completed_at,
               databases_discovered, databases_dumped, error_message)
           VALUES (?, ?, ?, 'manual', ?, ?, ?, ?, ?)""",
        (run_id, job_id, status, started_at, completed_at, databases_discovered, databases_dumped, error_message),
    )
    if target_status:
        await _exec(
            client,
            "INSERT INTO job_run_targets (run_id, target_id, status, snapshot_id) VALUES (?, 't-any', ?, 'abcd1234')",
            (run_id, target_status),
        )


async def _add_discovered_database(client, container: str = "pg", db_name: str = "appdb") -> None:
    databases = [{"container_name": container, "db_type": "postgres", "db_name": db_name, "host_path": None}]
    await _exec(
        client,
        "INSERT INTO discovered_containers (name, image, status, databases) VALUES (?, 'postgres:16', 'running', ?)",
        (container, json.dumps(databases)),
    )


async def _add_drill(
    client, drill_id: str, status: str, started_at: str, *, tested: int = 0, passed: int = 0, error: str | None = None
) -> None:
    await _exec(
        client,
        """INSERT INTO restore_drills (id, trigger, status, started_at, completed_at, databases_tested,
               databases_passed, databases_failed, error_message)
           VALUES (?, 'manual', ?, ?, ?, ?, ?, ?, ?)""",
        (
            drill_id,
            status,
            started_at,
            None if status == "running" else started_at,
            tested,
            passed,
            tested - passed,
            error,
        ),
    )


async def _add_target(client, target_id: str, name: str, target_type: str, enabled: int = 1) -> None:
    await _exec(
        client,
        "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES (?, ?, ?, ?, '{}')",
        (target_id, name, target_type, enabled),
    )


async def _add_verification(
    client, run_id: str, status: str, started_at: str, *, files_checked: int, error: str | None = None
) -> None:
    await _exec(
        client,
        """INSERT INTO verification_runs (id, target_id, started_at, completed_at, status, files_checked,
               files_passed, error_message)
           VALUES (?, 't-local', ?, ?, ?, ?, ?, ?)""",
        (run_id, started_at, None if status == "running" else started_at, status, files_checked, files_checked, error),
    )


async def _add_channel(client, channel_id: str, name: str, enabled: int = 1) -> None:
    await _exec(
        client,
        "INSERT INTO notification_channels (id, type, name, enabled, config, events) VALUES (?, 'ntfy', ?, ?, '{}', ?)",
        (channel_id, name, enabled, json.dumps(["backup.failed"])),
    )


async def _checklist(client, headers) -> dict:
    resp = await client.get(URL, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _steps(checklist: dict) -> dict:
    return {step["id"]: step for step in checklist["steps"]}


@pytest.fixture
async def headers(client):
    data = await do_setup(client)
    return auth_headers(data["api_key"])


async def _complete_everything(client, headers) -> None:
    await _add_run(client, "r-good", "flash", "success", "2026-11-01T03:00:00Z")
    await _add_target(client, "t-local", "Array", "local")
    await _add_target(client, "t-b2", "B2 Offsite", "b2")
    await _add_verification(client, "v1", "passed", "2026-11-01T04:00:00Z", files_checked=1)
    await _add_channel(client, "ch1", "Phone")
    resp = await client.post("/api/settings/recovery-kit/confirm", headers=headers)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth and shape
# ---------------------------------------------------------------------------


async def test_requires_auth(client):
    # Closed before setup too: only the wizard's scans are open then.
    assert (await client.get(URL)).status_code == 401
    await do_setup(client)
    assert (await client.get(URL)).status_code == 401
    assert (await client.post(f"{URL}/dismiss")).status_code == 401


async def test_fresh_install_has_every_step_to_do(client, headers):
    checklist = await _checklist(client, headers)

    assert checklist["complete"] is False
    assert checklist["completed_count"] == 0
    assert checklist["total"] == 5
    assert checklist["dismissed"] is False
    assert checklist["dismissed_at"] is None
    assert [step["id"] for step in checklist["steps"]] == list(STEP_IDS)
    for step in checklist["steps"]:
        assert set(step) >= {"id", "state", "done", "detail", "completed_at"}
        assert step["state"] == "todo"
        assert step["done"] is False
    # No databases yet: the restore test falls back to a verification restore.
    assert _steps(checklist)["restore_test"]["method"] == "verification"


# ---------------------------------------------------------------------------
# first_backup
# ---------------------------------------------------------------------------


async def test_successful_run_completes_first_backup(client, headers):
    await _add_run(client, "r1", "full", "success", "2026-11-01T03:00:00Z", databases_dumped=2)

    step = _steps(await _checklist(client, headers))["first_backup"]
    assert step["state"] == "done"
    assert step["done"] is True
    assert step["completed_at"] == "2026-11-01T03:05:00Z"


async def test_partial_run_with_snapshot_counts_like_the_freshness_watchdog(client, headers):
    # restic exit 3: snapshot written, some files unreadable. Still a restore point.
    await _add_run(client, "r1", "flash", "partial", "2026-11-01T03:00:00Z", target_status="partial")

    assert _steps(await _checklist(client, headers))["first_backup"]["state"] == "done"


async def test_partial_db_run_that_dumped_nothing_does_not_count(client, headers):
    # A database job on a system that has had databases must dump at least one.
    await _add_run(client, "r0", "db_dump", "failed", "2026-10-01T03:00:00Z", databases_dumped=1)
    await _add_run(
        client,
        "r1",
        "db_dump",
        "partial",
        "2026-11-01T03:00:00Z",
        target_status="success",
        databases_discovered=0,
        error_message="Database discovery failed",
    )

    step = _steps(await _checklist(client, headers))["first_backup"]
    assert step["state"] == "attention"
    assert step["detail"] == "The last backup was only partial: Database discovery failed"


async def test_partial_run_without_snapshot_needs_attention(client, headers):
    await _add_run(client, "r1", "flash", "partial", "2026-11-01T03:00:00Z", target_status="failed")

    step = _steps(await _checklist(client, headers))["first_backup"]
    assert step["state"] == "attention"
    assert step["detail"].startswith("The last backup was only partial:")


async def test_failed_run_needs_attention_with_reason(client, headers):
    await _add_run(client, "r1", "full", "failed", "2026-11-01T03:00:00Z", error_message="No enabled storage targets")

    step = _steps(await _checklist(client, headers))["first_backup"]
    assert step["state"] == "attention"
    assert step["done"] is False
    assert step["detail"] == "The last backup failed: No enabled storage targets"


async def test_running_backup_is_reported(client, headers):
    await _add_run(client, "r1", "full", "running", "2026-11-01T03:00:00Z")

    assert _steps(await _checklist(client, headers))["first_backup"]["state"] == "running"


async def test_earlier_good_run_stays_done_after_a_later_failure(client, headers):
    await _add_run(client, "r1", "flash", "success", "2026-11-01T03:00:00Z")
    await _add_run(client, "r2", "flash", "failed", "2026-11-02T03:00:00Z", error_message="boom")

    # Ongoing failures are the freshness watchdog's job, not the setup checklist's.
    assert _steps(await _checklist(client, headers))["first_backup"]["state"] == "done"


# ---------------------------------------------------------------------------
# restore_test
# ---------------------------------------------------------------------------


async def test_passed_drill_completes_restore_test(client, headers):
    await _add_discovered_database(client)
    await _add_drill(client, "d1", "passed", "2026-11-01T07:00:00Z", tested=2, passed=2)

    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["method"] == "drill"
    assert step["state"] == "done"
    assert step["detail"] == "2 of 2 databases restored in the last drill"
    assert step["completed_at"] == "2026-11-01T07:00:00Z"


async def test_latest_failed_drill_overrides_an_older_pass(client, headers):
    await _add_discovered_database(client)
    await _add_drill(client, "d1", "passed", "2026-11-01T07:00:00Z", tested=1, passed=1)
    await _add_drill(
        client, "d2", "failed", "2026-11-08T07:00:00Z", tested=1, passed=0, error="1 database(s) did not restore: pg"
    )

    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["state"] == "attention"
    assert step["detail"] == "1 database(s) did not restore: pg"


async def test_skipped_drills_do_not_count_and_explain_why(client, headers):
    await _add_discovered_database(client)
    await _add_drill(client, "d1", "skipped", "2026-11-01T07:00:00Z", error="no snapshot contains database dumps")

    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["state"] == "todo"
    assert step["detail"] == "The last drill was skipped: no snapshot contains database dumps"


async def test_running_drill_is_reported_until_one_passes(client, headers):
    await _add_discovered_database(client)
    await _add_drill(client, "d1", "running", "2026-11-01T07:00:00Z")
    assert _steps(await _checklist(client, headers))["restore_test"]["state"] == "running"

    await _add_drill(client, "d0", "passed", "2026-10-25T07:00:00Z", tested=1, passed=1)
    assert _steps(await _checklist(client, headers))["restore_test"]["state"] == "done"


async def test_databases_seen_only_in_past_runs_still_need_a_drill(client, headers):
    # Discovery is down right now, but this system has dumped databases before:
    # a file-level verification must not stand in for a drill.
    await _add_run(client, "r1", "full", "success", "2026-11-01T03:00:00Z", databases_dumped=3)
    await _add_target(client, "t-local", "Array", "local")
    await _add_verification(client, "v1", "passed", "2026-11-01T04:00:00Z", files_checked=1)

    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["method"] == "drill"
    assert step["state"] == "todo"


async def test_excluded_databases_do_not_require_a_drill(client, headers):
    await _add_discovered_database(client)
    excluded = [{"container_name": "pg", "db_type": "postgres", "db_name": "appdb", "host_path": None}]
    await _exec(
        client,
        "INSERT INTO settings (key, value) VALUES ('excluded_databases', ?)",
        (json.dumps(excluded),),
    )

    assert _steps(await _checklist(client, headers))["restore_test"]["method"] == "verification"


async def test_verification_restore_test_counts_without_databases(client, headers):
    await _add_target(client, "t-local", "Array", "local")
    # A verification whose restore test was skipped proves nothing about restores.
    await _add_verification(client, "v1", "passed", "2026-11-01T04:00:00Z", files_checked=0)
    assert _steps(await _checklist(client, headers))["restore_test"]["state"] == "todo"

    await _add_verification(client, "v2", "passed", "2026-11-02T04:00:00Z", files_checked=1)
    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["state"] == "done"
    assert step["method"] == "verification"
    assert step["completed_at"] == "2026-11-02T04:00:00Z"


async def test_failed_verification_needs_attention(client, headers):
    await _add_target(client, "t-local", "Array", "local")
    await _add_verification(client, "v1", "failed", "2026-11-01T04:00:00Z", files_checked=1, error="checksum mismatch")

    step = _steps(await _checklist(client, headers))["restore_test"]
    assert step["state"] == "attention"
    assert step["detail"] == "checksum mismatch"


# ---------------------------------------------------------------------------
# offsite_target / notifications
# ---------------------------------------------------------------------------


async def test_offsite_target_requires_an_enabled_non_local_target(client, headers):
    await _add_target(client, "t-local", "Array", "local")
    step = _steps(await _checklist(client, headers))["offsite_target"]
    assert step["state"] == "todo"
    assert step["detail"] == "Only local targets are configured: Array"

    await _add_target(client, "t-b2", "B2 Offsite", "b2", enabled=0)
    step = _steps(await _checklist(client, headers))["offsite_target"]
    assert step["state"] == "todo"
    assert step["detail"] == "Disabled: B2 Offsite"

    await _exec(client, "UPDATE storage_targets SET enabled = 1 WHERE id = 't-b2'")
    step = _steps(await _checklist(client, headers))["offsite_target"]
    assert step["state"] == "done"
    assert step["detail"] == "B2 Offsite"


async def test_notifications_require_an_enabled_channel(client, headers):
    await _add_channel(client, "ch1", "Phone", enabled=0)
    step = _steps(await _checklist(client, headers))["notifications"]
    assert step["state"] == "todo"
    assert step["detail"] == "Disabled: Phone"

    await _add_channel(client, "ch2", "Discord")
    step = _steps(await _checklist(client, headers))["notifications"]
    assert step["state"] == "done"
    assert step["detail"] == "Discord"


# ---------------------------------------------------------------------------
# recovery_kit
# ---------------------------------------------------------------------------


async def test_recovery_kit_confirmation_and_password_change(client, headers):
    resp = await client.post("/api/settings/recovery-kit/confirm", headers=headers)
    confirmed_at = resp.json()["confirmed_at"]
    step = _steps(await _checklist(client, headers))["recovery_kit"]
    assert step["state"] == "done"
    assert step["completed_at"] == confirmed_at

    # A kit saved before the current password can't open the backups.
    await _exec(client, "UPDATE settings SET updated_at = '2999-01-01T00:00:00Z' WHERE key = 'encryption_password'")
    step = _steps(await _checklist(client, headers))["recovery_kit"]
    assert step["state"] == "attention"
    assert "password changed" in step["detail"]


# ---------------------------------------------------------------------------
# Dismissal
# ---------------------------------------------------------------------------


async def test_dismiss_is_refused_until_every_step_is_done(client, headers):
    await _add_run(client, "r-good", "flash", "success", "2026-11-01T03:00:00Z")

    resp = await client.post(f"{URL}/dismiss", headers=headers)
    assert resp.status_code == 409
    assert "restore_test" in resp.text
    assert (await _checklist(client, headers))["dismissed"] is False


async def test_dismiss_once_complete_persists(client, headers):
    await _complete_everything(client, headers)
    checklist = await _checklist(client, headers)
    assert checklist["complete"] is True
    assert checklist["completed_count"] == 5

    resp = await client.post(f"{URL}/dismiss", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["dismissed"] is True
    assert body["dismissed_at"]

    again = await _checklist(client, headers)
    assert again["dismissed"] is True
    assert again["dismissed_at"] == body["dismissed_at"]

    # Dismissal sticks even if a step later regresses; dismissing again is a no-op.
    await _exec(client, "UPDATE notification_channels SET enabled = 0")
    resp = await client.post(f"{URL}/dismiss", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["dismissed_at"] == body["dismissed_at"]

    activity = await client.get("/api/activity", headers=headers)
    messages = [entry["message"] for entry in activity.json()["activities"]]
    assert "Setup checklist completed: this server is protected" in messages
