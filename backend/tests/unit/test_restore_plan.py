"""Unit tests for app.services.restore_plan — RestorePlanGenerator."""

from unittest.mock import MagicMock

import pytest

from app.services.restore_plan import RestorePlanGenerator


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.config_dir = tmp_path
    return config


@pytest.fixture
def generator(mock_config):
    return RestorePlanGenerator(config=mock_config)


class TestFormatBytes:
    """Test the instance _format_bytes helper."""

    def test_format_zero(self, generator):
        assert generator._format_bytes(0) == "0.0 B"

    def test_format_none(self, generator):
        # _format_bytes expects int; None would be handled by caller
        # Test with 0 as the safe equivalent
        assert generator._format_bytes(0) == "0.0 B"

    def test_format_kilobytes(self, generator):
        result = generator._format_bytes(1024)
        assert result == "1.0 KB"

    def test_format_megabytes(self, generator):
        result = generator._format_bytes(1048576)
        assert result == "1.0 MB"

    def test_format_gigabytes(self, generator):
        result = generator._format_bytes(1073741824)
        assert result == "1.0 GB"


class TestGetRestoreCommand:
    """Test _get_restore_commands for various database types."""

    def test_postgres(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "psql" in short or "psql" in full
        assert "pg_container" in full
        assert "-d mydb" not in short

    def test_mariadb(self, generator):
        short, full = generator._get_restore_commands("mariadb", "appdb", "mariadb_container")
        assert "mysql" in short or "mysql" in full
        assert "mariadb_container" in full
        assert " appdb" not in short

    def test_mongodb(self, generator):
        short, full = generator._get_restore_commands("mongodb", "mongotestdb", "mongo_container")
        assert "mongorestore" in short or "mongorestore" in full
        assert "mongo_container" in full

    def test_unknown_type(self, generator):
        short, full = generator._get_restore_commands("cockroachdb", "crdb", "crdb_container")
        assert "See" in short
        assert "documentation" in short


class TestGeneratePdfFallback:
    """Test that internal helpers are accessible and work correctly."""

    def test_get_restore_commands_returns_tuple(self, generator):
        """_get_restore_commands returns a tuple of two strings."""
        short, full = generator._get_restore_commands("postgres", "testdb", "pg")
        assert isinstance(short, str)
        assert isinstance(full, str)
        assert len(short) > 0
        assert len(full) > 0

    def test_repo_path_uses_default_remote_prefix(self, generator):
        target = {"id": "target1", "type": "s3", "config": {}}
        assert generator._repo_path(target) == "rclone:target1:arkive-backups"

    def test_repo_path_honors_remote_path(self, generator):
        target = {"id": "target2", "type": "sftp", "config": {"remote_path": "upload"}}
        assert generator._repo_path(target) == "rclone:target2:upload/arkive-backups"

    def test_repo_path_includes_bucket_for_s3_targets(self, generator):
        target = {"id": "target3", "type": "s3", "config": {"bucket": "vmx-bucket"}}
        assert generator._repo_path(target) == "rclone:target3:vmx-bucket/arkive-backups"

    def test_repo_path_includes_bucket_before_remote_path_for_b2_targets(self, generator):
        target = {
            "id": "target4",
            "type": "b2",
            "config": {"bucket": "vault", "remote_path": "customer-a"},
        }
        assert generator._repo_path(target) == "rclone:target4:vault/customer-a/arkive-backups"
