"""Comprehensive verification test suite.

Covers all 10 verification test areas:
1. Verification engine picks correct snapshots per target
2. File-level SHA-256 verification passes/fails correctly
3. Database dump validation (SQLite integrity, pg_restore --list)
4. Trust score computation algorithm
5. Scheduler job registration and execution
6. API endpoint returns correct verification data
7. Status endpoint includes verification fields
8. Verification respects backup lock
9. Notification fires on verification failure
10. Mock.ts contract test includes verification fields

Items 1-5 and 8 are covered in tests/unit/test_verify_engine.py.
Items 6, 7 are covered in tests/integration/test_api_verification.py.
This file adds coverage for items 9, 10, and strengthens cross-cutting areas.
"""

import json
import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from tests.conftest import auth_headers, do_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_TS_PATH = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "api" / "mock.ts"


async def _create_target(client, api_key, tmp_path, name="TestTarget"):
    """Helper: create a local storage target and return its ID."""
    target_path = str(tmp_path / f"backups-{name}")
    os.makedirs(target_path, exist_ok=True)
    resp = await client.post(
        "/api/targets",
        json={"name": name, "type": "local", "config": {"path": target_path}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _get_db_path(client):
    """Extract DB path from the test client's app config."""
    transport = client._transport
    app = transport.app
    return str(app.state.config.db_path)


async def _insert_verification_run(db_path, run_id, target_id, status="passed",
                                    trust_score=85, started_at="2026-03-15T10:00:00Z",
                                    completed_at="2026-03-15T10:05:00Z"):
    """Insert a verification_runs row for testing."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO verification_runs
               (id, target_id, started_at, completed_at, status, trust_score,
                files_checked, files_passed, databases_checked, databases_passed,
                restic_check_passed)
               VALUES (?, ?, ?, ?, ?, ?, 10, 10, 3, 3, 1)""",
            (run_id, target_id, started_at, completed_at, status, trust_score),
        )
        await db.commit()


# ===========================================================================
# 9. Notification fires on verification failure
# ===========================================================================


@pytest.mark.asyncio
class TestVerificationNotification:
    """Verify that the scheduler sends notifications when verification fails."""

    @pytest.fixture
    def mock_config(self, tmp_path):
        config = MagicMock()
        db_path = tmp_path / "arkive.db"
        config.db_path = str(db_path)
        config.config_dir = tmp_path
        config.dump_dir = tmp_path / "dumps"

        from app.core.database import SCHEMA_SQL
        from app.core.security import _load_fernet_from_dir, _reset_fernet
        _reset_fernet()
        _load_fernet_from_dir(str(tmp_path))

        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()
        return config

    @pytest.fixture
    def mock_notifier(self):
        notifier = MagicMock()
        notifier.send = AsyncMock(return_value=[{"channel_id": "ch1", "status": "sent"}])
        return notifier

    @pytest.fixture
    def mock_verify_engine_failed(self):
        """VerifyEngine that returns a failed target result."""
        ve = MagicMock()
        ve.verify_all_targets = AsyncMock(return_value={
            "status": "completed",
            "overall_trust_score": 25.0,
            "targets": [
                {
                    "target_id": "t1",
                    "target_name": "Local Backup",
                    "status": "failed",
                    "trust_score": 25.0,
                    "error": "restic check failed",
                },
            ],
        })
        return ve

    @pytest.fixture
    def mock_verify_engine_success(self):
        """VerifyEngine that returns a successful result."""
        ve = MagicMock()
        ve.verify_all_targets = AsyncMock(return_value={
            "status": "completed",
            "overall_trust_score": 95.0,
            "targets": [
                {
                    "target_id": "t1",
                    "target_name": "Local Backup",
                    "status": "completed",
                    "trust_score": 95.0,
                },
            ],
        })
        return ve

    @pytest.fixture
    def mock_verify_engine_low_score(self):
        """VerifyEngine that returns a low trust score (below 50)."""
        ve = MagicMock()
        ve.verify_all_targets = AsyncMock(return_value={
            "status": "completed",
            "overall_trust_score": 35.0,
            "targets": [
                {
                    "target_id": "t1",
                    "target_name": "B2 Cloud",
                    "status": "completed",
                    "trust_score": 35.0,
                },
            ],
        })
        return ve

    async def test_notification_sent_on_failed_target(
        self, mock_config, mock_notifier, mock_verify_engine_failed,
    ):
        """Notification is sent when a target verification fails."""
        from app.services.scheduler import ArkiveScheduler

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_failed,
            notifier=mock_notifier,
        )
        await sched._run_integrity_verify()

        mock_notifier.send.assert_awaited_once()
        call_args = mock_notifier.send.call_args
        assert call_args[0][0] == "verification.failed"
        assert "Local Backup" in call_args[0][2]

    async def test_notification_not_sent_on_success(
        self, mock_config, mock_notifier, mock_verify_engine_success,
    ):
        """No notification is sent when all targets pass verification."""
        from app.services.scheduler import ArkiveScheduler

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_success,
            notifier=mock_notifier,
        )
        await sched._run_integrity_verify()

        mock_notifier.send.assert_not_awaited()

    async def test_notification_sent_on_low_trust_score(
        self, mock_config, mock_notifier, mock_verify_engine_low_score,
    ):
        """Notification is sent when trust score is below 50."""
        from app.services.scheduler import ArkiveScheduler

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_low_score,
            notifier=mock_notifier,
        )
        await sched._run_integrity_verify()

        mock_notifier.send.assert_awaited_once()
        call_args = mock_notifier.send.call_args
        assert call_args[0][0] == "verification.failed"
        assert "B2 Cloud" in call_args[0][2]

    async def test_notification_skipped_without_notifier(
        self, mock_config, mock_verify_engine_failed,
    ):
        """No crash when notifier is None and verification fails."""
        from app.services.scheduler import ArkiveScheduler

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_failed,
            notifier=None,
        )
        # Should not raise
        await sched._run_integrity_verify()

    async def test_notification_error_does_not_crash_scheduler(
        self, mock_config, mock_verify_engine_failed,
    ):
        """Notification send failure does not crash the scheduler job."""
        from app.services.scheduler import ArkiveScheduler

        notifier = MagicMock()
        notifier.send = AsyncMock(side_effect=RuntimeError("notification service down"))

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_failed,
            notifier=notifier,
        )
        # Should not raise despite notification failure
        await sched._run_integrity_verify()

    async def test_notification_includes_trust_score(
        self, mock_config, mock_notifier, mock_verify_engine_failed,
    ):
        """Notification body includes the trust score value."""
        from app.services.scheduler import ArkiveScheduler

        orch = MagicMock()
        sched = ArkiveScheduler(
            orchestrator=orch,
            config=mock_config,
            verify_engine=mock_verify_engine_failed,
            notifier=mock_notifier,
        )
        await sched._run_integrity_verify()

        call_args = mock_notifier.send.call_args
        body = call_args[0][2]
        assert "25.0" in body  # trust score should be in body


# ===========================================================================
# 10. Mock.ts contract test includes verification fields
# ===========================================================================


def _parse_demo_status_keys() -> set[str]:
    """Extract top-level keys from DEMO_STATUS in mock.ts."""
    if not MOCK_TS_PATH.exists():
        pytest.skip(f"mock.ts not found at {MOCK_TS_PATH}")

    content = MOCK_TS_PATH.read_text()

    start_match = re.search(r'const DEMO_STATUS\s*=\s*\{', content)
    if not start_match:
        pytest.fail("Could not find DEMO_STATUS in mock.ts")

    brace_start = start_match.end() - 1
    depth = 0
    block_end = brace_start
    for i in range(brace_start, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                block_end = i
                break
    else:
        pytest.fail("Could not find closing brace for DEMO_STATUS")

    block = content[brace_start + 1:block_end]

    keys = set()
    current_depth = 0
    for line in block.split('\n'):
        stripped = line.strip()
        if current_depth == 0:
            m = re.match(r'(\w+)\s*:', stripped)
            if m:
                keys.add(m.group(1))
        current_depth += stripped.count('{') - stripped.count('}')

    return keys


class TestMockTsVerificationContract:
    """Verify that mock.ts includes verification-related fields."""

    def test_demo_status_has_trust_score(self):
        """DEMO_STATUS must include trust_score field."""
        keys = _parse_demo_status_keys()
        assert "trust_score" in keys, \
            "DEMO_STATUS missing trust_score -- dashboard trust badge won't render in demo mode"

    def test_demo_status_has_last_verified_at(self):
        """DEMO_STATUS must include last_verified_at field."""
        keys = _parse_demo_status_keys()
        assert "last_verified_at" in keys, \
            "DEMO_STATUS missing last_verified_at -- verification timing won't show in demo mode"

    def test_demo_status_has_verification_status(self):
        """DEMO_STATUS must include verification_status object."""
        keys = _parse_demo_status_keys()
        assert "verification_status" in keys, \
            "DEMO_STATUS missing verification_status -- verification widget won't render in demo mode"

    def test_verification_status_has_required_subfields(self):
        """verification_status in mock must have trust_score, verification_passing, last_verified_at."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")

        # Find verification_status block
        match = re.search(r'verification_status\s*:\s*\{([^}]+)\}', content)
        assert match, "verification_status object not found in mock.ts"

        block = match.group(1)
        assert "trust_score" in block, "verification_status missing trust_score"
        assert "verification_passing" in block, "verification_status missing verification_passing"
        assert "last_verified_at" in block, "verification_status missing last_verified_at"

    def test_mock_has_verification_get_route(self):
        """Mock must handle GET /verification route."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")
        assert "/verification" in content, "Mock missing /verification GET route"

    def test_mock_verification_get_returns_trust_score(self):
        """Mock GET /verification response must include trust_score."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")

        # The mock /verification handler should return trust_score in its response
        # Find the verification handler block
        idx = content.find("'/verification'")
        if idx == -1:
            idx = content.find('"/verification"')
        assert idx != -1, "Cannot find /verification route handler in mock"

        # Check the surrounding block for trust_score
        handler_block = content[idx:idx + 500]
        assert "trust_score" in handler_block, \
            "Mock /verification handler does not return trust_score"

    def test_mock_has_verification_run_route(self):
        """Mock must handle POST /verification/run route."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")
        assert "/verification/run" in content, "Mock missing /verification/run POST route"

    def test_mock_has_get_verification_method(self):
        """Mock API client must expose getVerification method."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")
        assert "getVerification" in content, "Mock missing getVerification method"

    def test_mock_has_trigger_verification_method(self):
        """Mock API client must expose triggerVerification method."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        if not content:
            pytest.skip("mock.ts not found")
        assert "triggerVerification" in content, "Mock missing triggerVerification method"


# ===========================================================================
# Cross-cutting: real API returns verification fields matching mock contract
# ===========================================================================


@pytest.mark.asyncio
class TestApiVerificationContract:
    """Verify that real API responses include all verification fields the mock promises."""

    async def test_status_api_has_all_verification_fields(self, client):
        """GET /api/status must include trust_score, last_verified_at, verification_status."""
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()

        for field in ("trust_score", "last_verified_at", "verification_status"):
            assert field in body, f"Status API missing field: {field}"

    async def test_status_verification_defaults(self, client):
        """With no verification runs, defaults are sensible (0 score, None status)."""
        resp = await client.get("/api/status")
        body = resp.json()
        assert body["trust_score"] == 0
        assert body["last_verified_at"] is None
        assert body["verification_status"] is None

    async def test_status_verification_status_object_shape(self, client, tmp_path):
        """When verification data exists, verification_status has the expected shape."""
        data = await do_setup(client)
        api_key = data["api_key"]
        target_id = await _create_target(client, api_key, tmp_path)
        db_path = await _get_db_path(client)

        await _insert_verification_run(db_path, "vr-shape", target_id, "passed", 88)

        resp = await client.get("/api/status")
        body = resp.json()
        vs = body["verification_status"]

        assert vs is not None
        assert "trust_score" in vs
        assert "last_verified_at" in vs
        assert "verification_passing" in vs
        assert vs["trust_score"] == 88
        assert vs["verification_passing"] is True

    async def test_verification_api_auth_required(self, client):
        """Both verification endpoints require authentication after setup."""
        await do_setup(client)

        resp_get = await client.get("/api/verification")
        assert resp_get.status_code == 401

        resp_post = await client.post("/api/verification/run")
        assert resp_post.status_code == 401

    async def test_verification_get_response_shape(self, client, tmp_path):
        """GET /api/verification returns trust_score and results list."""
        data = await do_setup(client)
        api_key = data["api_key"]

        resp = await client.get(
            "/api/verification", headers=auth_headers(api_key)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "trust_score" in body
        assert "results" in body
        assert isinstance(body["results"], list)

    async def test_verification_run_response_shape(self, client, tmp_path):
        """POST /api/verification/run returns expected fields."""
        data = await do_setup(client)
        api_key = data["api_key"]
        await _create_target(client, api_key, tmp_path)

        resp = await client.post(
            "/api/verification/run", headers=auth_headers(api_key)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert body["status"] == "started"
        assert "run_ids" in body
        assert "target_count" in body
        assert "message" in body

    async def test_failed_verification_reflected_in_status(self, client, tmp_path):
        """A failed verification run is reflected correctly in status."""
        data = await do_setup(client)
        api_key = data["api_key"]
        target_id = await _create_target(client, api_key, tmp_path)
        db_path = await _get_db_path(client)

        await _insert_verification_run(
            db_path, "vr-fail", target_id, "failed", 20,
        )

        resp = await client.get("/api/status")
        body = resp.json()
        assert body["trust_score"] == 20
        assert body["verification_status"]["verification_passing"] is False
