"""restic option injection, and what a fresh (not yet set up) instance exposes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.services.backup_engine import BackupEngine, _validate_path, _validate_snapshot_id
from app.utils.subprocess_runner import CommandResult
from tests.conftest import auth_headers, do_setup

TARGET = {"type": "local", "config": {"path": "/data"}}


def _engine(tmp_path, monkeypatch, calls):
    import app.services.backup_engine as be

    async def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return CommandResult(returncode=0, stdout="", stderr="", duration_seconds=0.0, command="restic")

    monkeypatch.setattr(be, "run_command", fake_run)
    engine = BackupEngine(SimpleNamespace(rclone_config=tmp_path / "rclone.conf", db_path=tmp_path / "x.db"))
    engine._get_password = AsyncMock(return_value="pw")
    return engine


class TestResticArguments:
    async def test_ls_passes_snapshot_and_path_after_double_dash(self, tmp_path, monkeypatch):
        calls = []
        engine = _engine(tmp_path, monkeypatch, calls)
        await engine.ls(TARGET, "latest", "/config/dumps")
        cmd = calls[-1]
        assert cmd[-3:] == ["--", "latest", "/config/dumps"]

    async def test_ls_rejects_option_shaped_path(self, tmp_path, monkeypatch):
        calls = []
        engine = _engine(tmp_path, monkeypatch, calls)
        with pytest.raises(ValueError):
            await engine.ls(TARGET, "latest", "--password-command=touch /tmp/pwned")
        assert calls == []

    async def test_restore_binds_values_to_their_flags(self, tmp_path, monkeypatch):
        calls = []
        engine = _engine(tmp_path, monkeypatch, calls)
        await engine.restore(TARGET, "abc123de", paths=["/config/dumps/a.sql"], restore_to="/config/restores/x")
        cmd = calls[-1]
        assert "--target=/config/restores/x" in cmd
        assert "--include=/config/dumps/a.sql" in cmd
        assert cmd[-2:] == ["--", "abc123de"]

    @pytest.mark.parametrize("value", ["-r", "--password-command", "-abc"])
    def test_snapshot_id_cannot_start_with_dash(self, value):
        with pytest.raises(ValueError):
            _validate_snapshot_id(value)

    @pytest.mark.parametrize("value", ["-x", "--password-command=id", "/a\nb", "/a\x00b"])
    def test_path_rejects_options_and_control_characters(self, value):
        with pytest.raises(ValueError):
            _validate_path(value)

    def test_path_accepts_normal_paths(self):
        assert _validate_path("/mnt/user/appdata/my-app") == "/mnt/user/appdata/my-app"
        assert _validate_path("relative/dir-name") == "relative/dir-name"


class TestFreshInstance:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/jobs"),
            ("GET", "/api/targets"),
            ("POST", "/api/targets"),
            ("GET", "/api/settings"),
            ("POST", "/api/restore"),
            ("GET", "/api/events/stream"),
        ],
    )
    async def test_protected_routes_are_closed_before_setup(self, client, method, path):
        resp = await client.request(method, path, json={} if method == "POST" else None)
        assert resp.status_code == 401, resp.text

    @pytest.mark.parametrize("path", ["/api/discover/scan", "/api/directories/scan"])
    async def test_setup_wizard_scans_work_before_setup(self, client, path):
        resp = await client.post(path)
        assert resp.status_code != 401, resp.text

    async def test_setup_wizard_scans_need_auth_after_setup(self, client):
        data = await do_setup(client)
        assert (await client.post("/api/directories/scan")).status_code == 401
        resp = await client.post("/api/directories/scan", headers=auth_headers(data["api_key"]))
        assert resp.status_code != 401

    async def test_concurrent_setup_has_one_winner(self, client):
        body = {"run_first_backup": False, "encryption_password": "test-password"}
        responses = await asyncio.gather(*(client.post("/api/auth/setup", json=body) for _ in range(2)))
        assert sorted(r.status_code for r in responses) == [201, 409]


async def test_browse_rejects_option_shaped_path_with_400(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    from app.core.dependencies import get_config

    async with aiosqlite.connect(get_config().db_path) as conn:
        await conn.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES ('t1', 'L', 'local', 1, '{}')"
        )
        await conn.execute(
            "INSERT INTO snapshots (id, target_id, full_id, time) VALUES ('abc12345', 't1', ?, '2026-01-01T00:00:00Z')",
            ("abc12345" + "0" * 56,),
        )
        await conn.commit()

    # A real engine, so the request reaches path validation; restic never runs.
    engine = BackupEngine(get_config())
    engine._get_password = AsyncMock(return_value="pw")
    client._transport.app.state.backup_engine = engine

    for url in ("/api/snapshots/abc12345/browse", "/api/restore/browse/abc12345"):
        resp = await client.get(url, params={"path": "--password-command=id"}, headers=headers)
        assert resp.status_code == 400, (url, resp.text)
