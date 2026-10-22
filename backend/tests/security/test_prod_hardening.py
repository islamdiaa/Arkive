"""Security regression tests for the prod-hardening review fixes.

Covers: rclone config injection, fail-closed auth on DB errors, lockout only on
the failure path, scheme-agnostic Origin checks, trusted proxies, secret
redaction in command logs, the rclone-obscure argv fallback, and the minimal
unauthenticated /api/status payload.
"""

import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.dependencies as deps
from app.core.config import ArkiveConfig
from app.services.cloud_manager import CloudManager, RcloneConfigError
from app.utils.redact import redact_command
from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# rclone config injection
# ---------------------------------------------------------------------------

INJECTED_HOST = "nas.local\nssh = sh -c 'touch /tmp/pwned'"


async def test_create_target_rejects_newline_in_sftp_host(client):
    data = await do_setup(client)
    resp = await client.post(
        "/api/targets",
        json={"name": "evil", "type": "sftp",
              "config": {"host": INJECTED_HOST, "username": "u", "password": "p"}},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 422
    assert "control characters" in resp.text


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://s3.example.com\n[other]\ntype = local"),
    ("bucket", "bucket\rname"),
    ("secret_key", "abc\x00def"),
    ("region", "us-east-1\x1b"),
])
async def test_create_s3_target_rejects_control_chars(client, field, value):
    data = await do_setup(client)
    config = {"endpoint": "https://s3.example.com", "access_key": "AK", "secret_key": "SK", "bucket": "b"}
    config[field] = value
    resp = await client.post(
        "/api/targets",
        json={"name": "s3", "type": "s3", "config": config},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 422, resp.text


async def test_create_target_rejects_control_char_in_nested_value(client):
    data = await do_setup(client)
    resp = await client.post(
        "/api/targets",
        json={"name": "b2", "type": "b2",
              "config": {"key_id": "k", "app_key": "a", "bucket": "b", "extra": {"x": "1\n2"}}},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 422


async def test_sftp_per_field_validation(client):
    data = await do_setup(client)
    bad_port = await client.post(
        "/api/targets",
        json={"name": "s", "type": "sftp", "config": {"host": "nas.local", "username": "u", "port": "99999"}},
        headers=auth_headers(data["api_key"]),
    )
    assert bad_port.status_code == 422
    bad_host = await client.post(
        "/api/targets",
        json={"name": "s", "type": "sftp", "config": {"host": "nas local;rm", "username": "u"}},
        headers=auth_headers(data["api_key"]),
    )
    assert bad_host.status_code == 422


async def test_update_target_rejects_newline_in_config(client, tmp_path):
    data = await do_setup(client)
    api_key = data["api_key"]
    created = await client.post(
        "/api/targets",
        json={"name": "s3", "type": "s3",
              "config": {"endpoint": "https://e", "access_key": "AK", "secret_key": "SK", "bucket": "b"}},
        headers=auth_headers(api_key),
    )
    assert created.status_code == 201
    target_id = created.json()["id"]
    resp = await client.put(
        f"/api/targets/{target_id}",
        json={"config": {"endpoint": "https://e\nprovider = x", "access_key": "AK", "secret_key": "SK",
                         "bucket": "b"}},
        headers=auth_headers(api_key),
    )
    assert resp.status_code == 422


async def test_test_connection_inline_rejects_newline(client):
    data = await do_setup(client)
    resp = await client.post(
        "/api/targets/test-connection",
        json={"type": "sftp", "config": {"host": INJECTED_HOST, "username": "u"}},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "control characters" in resp.json()["message"]


async def test_setup_storage_with_newline_creates_no_target(client):
    resp = await client.post("/api/auth/setup", json={
        "encryption_password": "test-password",
        "storage": {"type": "sftp", "host": INJECTED_HOST, "username": "u"},
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["target_created"] is False
    assert any("control characters" in e for e in body["target_errors"])


async def test_pretty_printed_oauth_token_is_compacted(client):
    data = await do_setup(client)
    resp = await client.post(
        "/api/targets",
        json={"name": "dbx", "type": "dropbox", "config": {"token": '{\n  "access_token": "abc"\n}'}},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 201, resp.text


class TestCloudManagerRefusesUnsafeValues:
    @pytest.fixture
    def cm(self, tmp_path):
        return CloudManager(ArkiveConfig(config_dir=tmp_path))

    async def test_newline_value_raises_and_file_untouched(self, cm):
        await cm.write_target_config({"id": "good", "type": "b2", "config": {"key_id": "k", "app_key": "a"}})
        before = cm.rclone_config.read_text()
        with pytest.raises(RcloneConfigError):
            await cm.write_target_config({
                "id": "bad", "type": "s3",
                "config": {"access_key": "AK", "secret_key": "SK", "endpoint": "https://e\nenv_auth = true"},
            })
        assert cm.rclone_config.read_text() == before

    async def test_sftp_host_injection_never_reaches_file(self, cm):
        with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as run:
            run.return_value = MagicMock(returncode=0, stdout="OBS", stderr="")
            with pytest.raises(RcloneConfigError):
                await cm.write_target_config({
                    "id": "sftp1", "type": "sftp",
                    "config": {"host": INJECTED_HOST, "username": "u", "password": "p"},
                })
        assert not cm.rclone_config.exists() or "ssh =" not in cm.rclone_config.read_text()

    @pytest.mark.parametrize("bad_id", ["a]\n[b", "../x", "", "x" * 65, "id with space"])
    async def test_invalid_section_name_raises(self, cm, bad_id):
        with pytest.raises(RcloneConfigError):
            await cm.write_target_config({"id": bad_id, "type": "b2", "config": {"key_id": "k", "app_key": "a"}})


# ---------------------------------------------------------------------------
# rclone obscure: no argv fallback, fail loudly
# ---------------------------------------------------------------------------


async def test_obscure_failure_raises_without_argv_fallback(tmp_path):
    cm = CloudManager(ArkiveConfig(config_dir=tmp_path))
    with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        with pytest.raises(RuntimeError, match="rclone obscure failed"):
            await cm.write_target_config({
                "id": "sftp1", "type": "sftp",
                "config": {"host": "nas.local", "username": "u", "password": "hunter2-secret"},
            })
    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv == ["rclone", "obscure", "-"]
    assert "hunter2-secret" not in " ".join(argv)
    # Nothing (and certainly no plaintext pass) was written.
    assert not cm.rclone_config.exists()


async def test_obscure_timeout_raises(tmp_path):
    cm = CloudManager(ArkiveConfig(config_dir=tmp_path))
    with patch("app.services.cloud_manager.run_command", new_callable=AsyncMock) as run:
        run.return_value = MagicMock(returncode=-1, stdout="", stderr="Command timed out after 30s")
        with pytest.raises(RuntimeError):
            await cm.write_target_config({
                "id": "sftp1", "type": "sftp",
                "config": {"host": "nas.local", "username": "u", "password": "pw"},
            })
    assert run.call_count == 1


# ---------------------------------------------------------------------------
# Secret redaction in command logging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv,secret", [
    (["rclone", "obscure", "--", "hunter2"], "hunter2"),
    (["/usr/bin/rclone", "obscure", "hunter2"], "hunter2"),
    (["mysqldump", "-u", "root", "-phunter2", "db"], "hunter2"),
    (["mysqldump", "--password", "hunter2", "db"], "hunter2"),
    (["mysqldump", "--password=hunter2", "db"], "hunter2"),
    (["env", "PGPASSWORD=hunter2", "pg_dump"], "hunter2"),
    (["env", "MYSQL_PWD=hunter2", "mysqldump"], "hunter2"),
    (["rclone", "config", "create", "x", "sftp", "pass=hunter2"], "hunter2"),
    (["tool", "--token", "hunter2"], "hunter2"),
])
def test_redact_command_masks_secrets(argv, secret):
    rendered = redact_command(argv)
    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_redact_command_keeps_ordinary_args():
    rendered = redact_command(["restic", "-r", "rclone:abc:bucket/arkive-backups", "snapshots", "--json"])
    assert rendered == "restic -r rclone:abc:bucket/arkive-backups snapshots --json"


async def test_run_command_logs_redacted_argv(caplog):
    from app.utils.subprocess_runner import run_command

    caplog.set_level(logging.DEBUG, logger="arkive.subprocess")
    result = await run_command(
        ["sh", "-c", "exit 3", "--password", "s3cr3t-value"], timeout=10,
    )
    assert result.returncode == 3
    assert "s3cr3t-value" not in caplog.text
    assert "s3cr3t-value" not in result.command
    assert "[REDACTED]" in result.command


# ---------------------------------------------------------------------------
# require_auth fails closed on DB errors
# ---------------------------------------------------------------------------


class _RaisingCursorDB:
    def __init__(self, message):
        self.message = message

    async def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError(self.message)


async def test_load_api_key_hash_missing_table_is_bootstrap():
    assert await deps._load_api_key_hash(_RaisingCursorDB("no such table: settings")) == (True, None)


@pytest.mark.parametrize("message", ["database is locked", "disk I/O error", "unable to open database file"])
async def test_load_api_key_hash_other_errors_fail_closed(message):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await deps._load_api_key_hash(_RaisingCursorDB(message))
    assert exc.value.status_code == 503


async def test_protected_endpoint_returns_503_when_settings_unreadable(client):
    await do_setup(client)
    app = client._transport.app

    async def _locked_db():
        yield _RaisingCursorDB("database is locked")

    app.dependency_overrides[deps.get_db] = _locked_db
    try:
        resp = await client.get("/api/jobs")
        sse = await client.get("/api/events/stream")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
    assert resp.status_code == 503
    assert sse.status_code == 503


async def test_session_endpoint_returns_503_not_setup_mode_on_db_error(client):
    await do_setup(client)
    app = client._transport.app

    async def _locked_db():
        yield _RaisingCursorDB("database is locked")

    app.dependency_overrides[deps.get_db] = _locked_db
    try:
        resp = await client.get("/api/auth/session")
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Lockout only on the failure path; bounded state
# ---------------------------------------------------------------------------


async def test_valid_api_key_works_while_ip_locked_out(client):
    data = await do_setup(client)
    for _ in range(deps.RATE_LIMIT_MAX):
        await client.get("/api/jobs", headers=auth_headers("ark_bad"))
    locked = await client.get("/api/jobs", headers=auth_headers("ark_bad"))
    assert locked.status_code == 429

    ok = await client.get("/api/jobs", headers=auth_headers(data["api_key"]))
    assert ok.status_code == 200


async def test_valid_session_works_while_ip_locked_out(client):
    await do_setup(client, keep_session=True)
    for _ in range(deps.RATE_LIMIT_MAX):
        await client.get("/api/jobs", headers=auth_headers("ark_bad"))
    assert deps._is_locked_out("127.0.0.1")
    resp = await client.get("/api/jobs")  # cookie only
    assert resp.status_code == 200


async def test_login_with_valid_key_works_while_locked_out(client):
    data = await do_setup(client)
    for _ in range(deps.RATE_LIMIT_MAX):
        await client.post("/api/auth/login", json={"api_key": "ark_bad"})
    bad = await client.post("/api/auth/login", json={"api_key": "ark_bad"})
    assert bad.status_code == 429
    good = await client.post("/api/auth/login", json={"api_key": data["api_key"]})
    assert good.status_code == 200


def test_rate_limit_state_is_capped(monkeypatch):
    monkeypatch.setattr(deps, "MAX_TRACKED_IPS", 50)
    deps._failed_attempts.clear()
    deps._lockouts.clear()
    try:
        for i in range(200):
            deps._track_failed_attempt(f"10.1.{i // 250}.{i % 250}")
        assert len(deps._failed_attempts) <= 51
    finally:
        deps._failed_attempts.clear()
        deps._lockouts.clear()


def test_trusted_proxy_client_ip(monkeypatch):
    request = MagicMock()
    request.client.host = "172.17.0.5"
    request.headers = {"x-forwarded-for": "203.0.113.9, 172.17.0.5"}
    monkeypatch.delenv("ARKIVE_TRUSTED_PROXIES", raising=False)
    assert deps.client_ip(request) == "172.17.0.5"
    monkeypatch.setenv("ARKIVE_TRUSTED_PROXIES", "172.17.0.0/16")
    assert deps.client_ip(request) == "203.0.113.9"


# ---------------------------------------------------------------------------
# Origin check: host:port compared, scheme ignored
# ---------------------------------------------------------------------------


async def test_https_origin_behind_tls_proxy_is_allowed(client):
    await do_setup(client, keep_session=True)
    resp = await client.post("/api/auth/rotate-key", headers={"Origin": "https://test"})
    assert resp.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://test.evil.example", "null", "file://test"])
async def test_cross_site_origin_still_blocked(client, origin):
    await do_setup(client, keep_session=True)
    resp = await client.post("/api/auth/rotate-key", headers={"Origin": origin})
    assert resp.status_code == 403


async def test_missing_origin_and_referer_blocked(client):
    await do_setup(client, keep_session=True)
    resp = await client.post("/api/auth/rotate-key")
    assert resp.status_code == 403


async def test_forwarded_host_only_honored_from_trusted_proxy(client, monkeypatch):
    await do_setup(client, keep_session=True)
    headers = {"Origin": "https://arkive.example.com", "X-Forwarded-Host": "arkive.example.com"}
    monkeypatch.delenv("ARKIVE_TRUSTED_PROXIES", raising=False)
    assert (await client.post("/api/auth/rotate-key", headers=headers)).status_code == 403
    monkeypatch.setenv("ARKIVE_TRUSTED_PROXIES", "127.0.0.1")
    assert (await client.post("/api/auth/rotate-key", headers=headers)).status_code == 200


# ---------------------------------------------------------------------------
# /api/status: minimal when unauthenticated
# ---------------------------------------------------------------------------


async def test_status_unauthenticated_is_minimal_after_setup(client):
    await do_setup(client, directories=["/mnt/user/appdata"])
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status", "health", "version", "setup_completed", "authenticated"}
    assert body["setup_completed"] is True
    assert body["authenticated"] is False
    assert "coverage" not in body and "hostname" not in body


async def test_status_full_with_api_key_or_session(client):
    data = await do_setup(client, keep_session=True)
    via_cookie = (await client.get("/api/status")).json()
    assert via_cookie["authenticated"] is True
    assert "coverage" in via_cookie and "hostname" in via_cookie

    client.cookies.clear()
    via_key = (await client.get("/api/status", headers=auth_headers(data["api_key"]))).json()
    assert via_key["authenticated"] is True
    assert "targets" in via_key


async def test_status_invalid_key_is_minimal_and_not_counted_as_failure(client):
    await do_setup(client)
    resp = await client.get("/api/status", headers=auth_headers("ark_bad"))
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False
    assert "127.0.0.1" not in deps._failed_attempts


async def test_status_full_in_setup_mode(client):
    body = (await client.get("/api/status")).json()
    assert body["setup_completed"] is False
    assert "coverage" in body


async def test_status_hostname_resolved_off_event_loop(client):
    data = await do_setup(client)
    seen_threads = []

    def _fake_resolve(**kwargs):
        import threading
        seen_threads.append(threading.current_thread().name)
        return "tower"

    with patch("app.api.status.resolve_hostname", side_effect=_fake_resolve):
        body = (await client.get("/api/status", headers=auth_headers(data["api_key"]))).json()
    assert body["hostname"] == "tower"
    assert seen_threads and seen_threads[0] != "MainThread"
