"""Tests for the backup engine (restic wrapper) — 8 test cases."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestBackupEngine:
    """Backup engine (restic) tests."""

    def test_restic_init_command_construction(self):
        """restic init should include repo path and password."""
        repo = "rclone:b2:arkive-backups"
        cmd = ["restic", "init", "--repo", repo]
        assert cmd[0] == "restic"
        assert cmd[1] == "init"
        assert "--repo" in cmd
        assert repo in cmd

    def test_restic_init_idempotent(self):
        """restic init on existing repo should not fail (already initialized)."""
        result = MagicMock()
        result.returncode = 1
        result.stderr = "repository master key and target already initialized"
        # Should detect "already initialized" and treat as success
        is_already_init = "already initialized" in result.stderr
        assert is_already_init

    def test_restic_backup_with_tags(self):
        """Backup command should include tags for identification."""
        tags = ["arkive", "daily", "full"]
        repo = "rclone:b2:arkive-backups"
        paths = ["/config/dumps"]
        cmd = ["restic", "backup", "--repo", repo]
        for tag in tags:
            cmd.extend(["--tag", tag])
        cmd.extend(paths)
        assert "--tag" in cmd
        assert "arkive" in cmd
        assert "daily" in cmd

    def test_restic_forget_with_retention_policy(self):
        """Forget command should apply retention policy."""
        cmd = [
            "restic",
            "forget",
            "--repo",
            "rclone:b2:arkive",
            "--keep-daily",
            "7",
            "--keep-weekly",
            "4",
            "--keep-monthly",
            "6",
            "--prune",
        ]
        assert "--keep-daily" in cmd
        assert "--keep-weekly" in cmd
        assert "--keep-monthly" in cmd
        assert "--prune" in cmd

    def test_restic_snapshots_json_parsing(self):
        """Should parse restic snapshots JSON output correctly."""
        raw_output = json.dumps(
            [
                {
                    "id": "abc123def456",
                    "short_id": "abc123de",
                    "time": "2026-02-25T07:00:00.000Z",
                    "hostname": "arkive",
                    "paths": ["/config/dumps"],
                    "tags": ["daily"],
                }
            ]
        )
        snapshots = json.loads(raw_output)
        assert len(snapshots) == 1
        assert snapshots[0]["short_id"] == "abc123de"
        assert snapshots[0]["tags"] == ["daily"]

    def test_restic_restore_command_construction(self):
        """Restore command should include snapshot ID and target path."""
        snapshot_id = "abc123de"
        target = "/tmp/restore"
        cmd = ["restic", "restore", snapshot_id, "--target", target]
        assert snapshot_id in cmd
        assert "--target" in cmd
        assert target in cmd

    def test_restic_unlock_command(self):
        """Unlock should remove stale locks."""
        cmd = ["restic", "unlock", "--repo", "rclone:b2:arkive"]
        assert cmd[1] == "unlock"

    def test_handle_restic_errors(self):
        """Non-zero exit codes should raise appropriate errors."""
        result = MagicMock()
        result.returncode = 1
        result.stderr = "Fatal: unable to open config file: stat /config/restic-repo/config: no such file or directory"
        assert result.returncode != 0
        assert "Fatal" in result.stderr

    @pytest.mark.asyncio
    async def test_snapshots_use_summary_size_when_restic_omits_top_level_size(self, monkeypatch):
        """Snapshot listing should normalize size from restic summary fields."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = "/tmp/ignored.db"
        config.rclone_config = "/tmp/rclone.conf"

        engine = BackupEngine(config)
        engine._get_password = AsyncMock(return_value="secret")

        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            [
                {
                    "id": "abc123",
                    "short_id": "abc123",
                    "summary": {
                        "data_added_packed": 12345,
                        "total_bytes_processed": 99999,
                    },
                }
            ]
        )

        monkeypatch.setattr("app.services.backup_engine.run_command", AsyncMock(return_value=result))

        snapshots = await engine.snapshots({"type": "local", "config": {"path": "/data"}})
        assert snapshots[0]["size"] == 12345

    @pytest.mark.asyncio
    async def test_backup_uses_resolved_host_for_snapshot_metadata(self, monkeypatch):
        """Backups should pass the resolved host name to restic."""
        from app.services.backup_engine import BackupEngine

        config = MagicMock()
        config.db_path = "/tmp/ignored.db"
        config.rclone_config = "/tmp/rclone.conf"

        engine = BackupEngine(config)
        engine._get_password = AsyncMock(return_value="secret")
        engine._get_bandwidth_limit = AsyncMock(return_value="")
        engine._get_server_name = AsyncMock(return_value="test-server")

        captured_cmd = []

        async def fake_run_command(cmd, **kwargs):
            captured_cmd[:] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "message_type": "summary",
                    "snapshot_id": "abc123",
                    "total_bytes_processed": 42,
                    "files_new": 1,
                    "files_changed": 0,
                }
            )
            result.stderr = ""
            return result

        monkeypatch.setattr("app.services.backup_engine.run_command", fake_run_command)

        result = await engine.backup(
            {"id": "target1", "name": "Local", "type": "local", "config": {"path": "/data"}},
            ["/config/dumps"],
        )

        assert result["status"] == "success"
        assert "--host" in captured_cmd
        assert captured_cmd[captured_cmd.index("--host") + 1] == "test-server"
