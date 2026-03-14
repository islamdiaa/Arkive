"""Phase 9: CLI tests with mock Docker — tests CLI commands that hit the API."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from app.cli import cli


class TestCLIDiscover:
    """CLI discover commands — these use httpx to call the running API.

    Since we can't easily run a full server in CLI tests, we mock the
    httpx.Client to return realistic responses matching what FakeDockerClient
    would produce through the API.
    """

    def _mock_scan_response(self):
        """Response matching POST /api/discover/scan with FakeDockerClient."""
        return {
            "total_containers": 6,
            "running_containers": 6,
            "stopped_containers": 0,
            "containers": [
                {
                    "name": "fake-postgres",
                    "image": "postgres:16",
                    "status": "running",
                    "databases": [
                        {
                            "container_name": "fake-postgres",
                            "db_type": "postgres",
                            "db_name": "testdb",
                            "host_path": None,
                        }
                    ],
                    "profile": "postgres",
                    "priority": "high",
                    "ports": [],
                    "mounts": [],
                },
                {
                    "name": "fake-mariadb",
                    "image": "mariadb:11",
                    "status": "running",
                    "databases": [
                        {"container_name": "fake-mariadb", "db_type": "mariadb", "db_name": "appdb", "host_path": None}
                    ],
                    "profile": "mariadb",
                    "priority": "high",
                    "ports": [],
                    "mounts": [],
                },
                {
                    "name": "fake-mongo",
                    "image": "mongo:7",
                    "status": "running",
                    "databases": [
                        {"container_name": "fake-mongo", "db_type": "mongodb", "db_name": "myapp", "host_path": None}
                    ],
                    "profile": "mongodb",
                    "priority": "high",
                    "ports": [],
                    "mounts": [],
                },
                {
                    "name": "fake-redis",
                    "image": "redis:7",
                    "status": "running",
                    "databases": [
                        {"container_name": "fake-redis", "db_type": "redis", "db_name": "redis", "host_path": None}
                    ],
                    "profile": "redis",
                    "priority": "low",
                    "ports": [],
                    "mounts": [],
                },
                {
                    "name": "fake-vaultwarden",
                    "image": "vaultwarden/server:latest",
                    "status": "running",
                    "databases": [],
                    "profile": "vaultwarden",
                    "priority": "critical",
                    "ports": [],
                    "mounts": [],
                },
                {
                    "name": "fake-adguard",
                    "image": "adguard/adguardhome",
                    "status": "running",
                    "databases": [],
                    "profile": "adguard",
                    "priority": "medium",
                    "ports": [],
                    "mounts": [],
                },
            ],
            "databases": [
                {"container_name": "fake-postgres", "db_type": "postgres", "db_name": "testdb"},
                {"container_name": "fake-mariadb", "db_type": "mariadb", "db_name": "appdb"},
                {"container_name": "fake-mongo", "db_type": "mongodb", "db_name": "myapp"},
                {"container_name": "fake-redis", "db_type": "redis", "db_name": "redis"},
            ],
            "scan_duration_seconds": 0.01,
            "scanned_at": "2026-01-01T00:00:00Z",
        }

    def _mock_status_response(self):
        """Response matching GET /api/status."""
        return {
            "version": "3.0.0",
            "status": "ok",
            "platform": "unraid",
            "hostname": "test-server",
            "uptime_seconds": 100,
            "setup_completed": True,
            "targets": {"total": 1, "healthy": 1},
            "containers": {"total": 6, "running": 6},
            "last_backup": None,
        }

    def test_cli_discover_scan(self):
        """arkive discover scan → shows discovered containers."""
        runner = CliRunner()
        scan_resp = self._mock_scan_response()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = scan_resp
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.get.return_value = mock_response

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["discover", "scan"])

        assert result.exit_code == 0
        assert "6 containers" in result.output
        assert "4 databases" in result.output

    def test_cli_discover_scan_json(self):
        """arkive discover scan --json → valid JSON with containers."""
        runner = CliRunner()
        scan_resp = self._mock_scan_response()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = scan_resp
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post.return_value = mock_response

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["--json", "discover", "scan"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_containers"] == 6
        assert len(data["databases"]) == 4

    def test_cli_status_shows_containers(self):
        """arkive status → shows container count."""
        runner = CliRunner()
        status_resp = self._mock_status_response()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = status_resp
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        with patch("app.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "ok" in result.output.lower() or "Status" in result.output
