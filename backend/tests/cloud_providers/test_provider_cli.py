"""CLI tests for storage target commands with all providers.

Uses Click's CliRunner with mocked httpx.Client to simulate API responses
for target listing, backup triggers, and snapshot queries.
"""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from app.cli import cli
from tests.cloud_providers.conftest import ALL_PROVIDERS


def _make_targets_response():
    """Build a realistic GET /api/targets response with all providers."""
    targets = []
    for i, (key, spec) in enumerate(ALL_PROVIDERS.items()):
        targets.append(
            {
                "id": f"tgt-{key[:4]}",
                "name": spec["name"],
                "type": spec["type"],
                "status": "ok",
                "enabled": True,
                "snapshot_count": i + 1,
                "last_tested": "2026-02-28T10:00:00Z",
                "config": {"redacted": "***"},
            }
        )
    return {"items": targets, "targets": targets, "total": len(targets)}


def _make_jobs_response():
    """Build a GET /api/jobs response with a job targeting all providers."""
    return {
        "jobs": [
            {
                "id": "job-all",
                "name": "Full Backup All Targets",
                "schedule": "0 3 * * *",
                "enabled": True,
                "targets": [f"tgt-{k[:4]}" for k in ALL_PROVIDERS],
            },
        ],
        "total": 1,
    }


def _make_backup_run_response():
    """Build a POST /api/jobs/{id}/run response."""
    return {
        "run_id": "run-mock-001",
        "status": "running",
        "message": "Backup started",
    }


def _make_snapshots_response():
    """Build a GET /api/snapshots response with snapshots from multiple targets."""
    snapshots = []
    for key in ALL_PROVIDERS:
        snapshots.append(
            {
                "id": f"snap-{key[:4]}",
                "target_id": f"tgt-{key[:4]}",
                "target_name": ALL_PROVIDERS[key]["name"],
                "time": "2026-02-28T03:00:00Z",
                "hostname": "test-server",
                "tags": [f"provider:{key}"],
                "short_id": f"snap{key[:4]}",
            }
        )
    return {"snapshots": snapshots, "total": len(snapshots)}


def _make_mock_client(responses=None):
    """Create a mock httpx.Client that returns configured responses."""
    if responses is None:
        responses = {}

    mock_client = MagicMock()

    def _get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()

        if "/api/targets" in url:
            resp.json.return_value = responses.get("targets", _make_targets_response())
        elif "/api/jobs" in url:
            resp.json.return_value = responses.get("jobs", _make_jobs_response())
        elif "/api/snapshots" in url:
            resp.json.return_value = responses.get("snapshots", _make_snapshots_response())
        elif "/api/status" in url:
            resp.json.return_value = responses.get(
                "status",
                {
                    "version": "3.0.0",
                    "status": "ok",
                    "platform": "unraid",
                    "hostname": "test-server",
                    "uptime_seconds": 3600,
                    "setup_completed": True,
                    "targets": {"total": 8, "healthy": 8},
                    "containers": {"total": 0, "running": 0},
                    "last_backup": {
                        "status": "success",
                        "started_at": "2026-02-28T03:00:00Z",
                        "run_id": "run-001",
                    },
                },
            )
        else:
            resp.json.return_value = {}
        return resp

    def _post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()

        if "/run" in url:
            resp.json.return_value = _make_backup_run_response()
        else:
            resp.json.return_value = {"status": "ok"}
        return resp

    mock_client.get = MagicMock(side_effect=_get)
    mock_client.post = MagicMock(side_effect=_post)
    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCLITargets:
    """arkive targets — list all storage targets."""

    def test_targets_list_shows_all_providers(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["targets"])

        assert result.exit_code == 0, result.output
        # Each provider should appear in output
        for key, spec in ALL_PROVIDERS.items():
            assert spec["type"] in result.output, f"Provider {key} ({spec['type']}) missing from targets output"

    def test_targets_list_shows_status(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["targets", "list"])

        assert result.exit_code == 0
        assert "ok" in result.output

    def test_targets_list_json_mode(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["--json", "targets"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == len(ALL_PROVIDERS)
        assert len(data["targets"]) == len(ALL_PROVIDERS)

    def test_targets_empty(self):
        runner = CliRunner()
        mock_client = _make_mock_client(responses={"targets": {"items": [], "targets": [], "total": 0}})

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["targets"])

        assert result.exit_code == 0
        assert "No storage targets" in result.output


class TestCLIBackup:
    """arkive backup — trigger backup that uses all providers."""

    def test_backup_now_triggers_run(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["backup", "--now"])

        assert result.exit_code == 0
        assert "run-mock-001" in result.output

    def test_backup_now_json(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["--json", "backup", "--now"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["run_id"] == "run-mock-001"
        assert data["status"] == "running"

    def test_backup_now_with_job_id(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["backup", "--now", "--job-id", "job-all"])

        assert result.exit_code == 0
        # Verify the correct job was triggered
        mock_client.post.assert_called()
        call_url = mock_client.post.call_args[0][0]
        assert "job-all" in call_url

    def test_backup_list_shows_jobs(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["backup", "list"])

        assert result.exit_code == 0
        assert "Full Backup All Targets" in result.output


class TestCLISnapshots:
    """arkive snapshots — list snapshots across all providers."""

    def test_snapshots_list_shows_all(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["snapshots"])

        assert result.exit_code == 0
        # Should show snapshots from multiple targets
        for key, spec in ALL_PROVIDERS.items():
            short_id = f"snap{key[:4]}"
            assert short_id in result.output or spec["name"] in result.output or f"tgt-{key[:4]}" in result.output, (
                f"Snapshot for {key} missing from output"
            )

    def test_snapshots_json(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["--json", "snapshots"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == len(ALL_PROVIDERS)


class TestCLIStatus:
    """arkive status — shows target health across providers."""

    def test_status_shows_target_count(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        # Status should mention targets
        assert "8" in result.output or "target" in result.output.lower()

    def test_status_json(self):
        runner = CliRunner()
        mock_client = _make_mock_client()

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["--json", "status"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["targets"]["total"] == 8
        assert data["targets"]["healthy"] == 8
