"""Critical tests for DB dumper failure detection.

Validates that the DB dumper correctly detects and reports:
- Zero-byte dump output
- Non-zero exit codes
- Container not found errors
- Credential redaction in error messages
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper, DumpResult

logger = logging.getLogger(__name__)


@pytest.fixture
def dumper(tmp_path):
    """Create a DBDumper with mocked Docker client and temp dump dir."""
    docker_client = MagicMock()
    config = MagicMock()
    config.dump_dir = tmp_path / "dumps"
    config.dump_dir.mkdir(parents=True, exist_ok=True)
    return DBDumper(docker_client, config)


def _make_db(container_name="test-pg", db_type="postgres", db_name="testdb",
             host_path=None):
    """Create a test DiscoveredDatabase."""
    return DiscoveredDatabase(
        container_name=container_name,
        db_type=db_type,
        db_name=db_name,
        host_path=host_path,
    )


class TestZeroByteDumpDetection:
    """Test that zero-byte dumps are correctly detected as failures."""

    def test_postgres_zero_bytes_returns_failed(self, dumper):
        """Postgres dump with zero bytes written should report failed."""
        db = _make_db()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        # Simulate exec_run returning empty stdout
        container.exec_run = MagicMock(return_value=(0, iter([])))
        dumper.docker.containers.get = MagicMock(return_value=container)

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert result.dump_size_bytes == 0
        logger.info("Postgres zero-byte dump correctly detected as failed: %s", result.error)

    def test_mariadb_zero_bytes_returns_failed(self, dumper):
        """MariaDB dump with zero bytes written should report failed."""
        db = _make_db(container_name="test-maria", db_type="mariadb", db_name="app_db")
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["MYSQL_ROOT_PASSWORD=secret"]}}
        container.image.tags = ["mariadb:11"]
        container.exec_run = MagicMock(return_value=(0, iter([])))
        dumper.docker.containers.get = MagicMock(return_value=container)

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "failed"
        assert result.dump_size_bytes == 0
        logger.info("MariaDB zero-byte dump correctly detected as failed: %s", result.error)

    def test_mongodb_zero_bytes_returns_failed(self, dumper):
        """MongoDB dump with zero bytes written should report failed."""
        db = _make_db(container_name="test-mongo", db_type="mongodb", db_name="admin")
        container = MagicMock()
        container.attrs = {"Config": {"Env": []}}
        container.exec_run = MagicMock(return_value=(0, iter([])))
        dumper.docker.containers.get = MagicMock(return_value=container)

        result = dumper._dump_mongodb_blocking(db)

        assert result.status == "failed"
        assert result.dump_size_bytes == 0
        logger.info("MongoDB zero-byte dump correctly detected as failed: %s", result.error)


class TestNonZeroExitCode:
    """Test that non-zero exit codes from dump commands are reported."""

    def test_postgres_nonzero_exit_returns_failed(self, dumper):
        """Postgres pg_dump with exit code 1 should report failed."""
        db = _make_db()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        # Return exit code 1 with an error on stderr
        stderr_msg = b"pg_dump: error: connection refused"
        container.exec_run = MagicMock(return_value=(
            1, iter([(None, stderr_msg)])
        ))
        dumper.docker.containers.get = MagicMock(return_value=container)

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert "exit" in (result.error or "").lower() or "connection" in (result.error or "").lower()
        logger.info("Postgres non-zero exit detected: %s", result.error)

    def test_mariadb_nonzero_exit_returns_failed(self, dumper):
        """MariaDB mysqldump with exit code 2 should report failed."""
        db = _make_db(container_name="test-maria", db_type="mariadb", db_name="app_db")
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["MYSQL_ROOT_PASSWORD=secret"]}}
        container.image.tags = ["mariadb:11"]
        stderr_msg = b"mysqldump: Access denied for user 'root'"
        container.exec_run = MagicMock(return_value=(
            2, iter([(None, stderr_msg)])
        ))
        dumper.docker.containers.get = MagicMock(return_value=container)

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "failed"
        logger.info("MariaDB non-zero exit detected: %s", result.error)


class TestContainerErrors:
    """Test handling when container operations fail."""

    def test_container_not_found_returns_failed(self, dumper):
        """Attempting to dump a missing container should report failed."""
        db = _make_db()
        dumper.docker.containers.get = MagicMock(
            side_effect=Exception("No such container: test-pg")
        )

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert "No such container" in (result.error or "")
        logger.info("Container not found correctly reported: %s", result.error)

    async def test_unsupported_db_type_returns_failed(self, dumper):
        """Unsupported database type should return failed with clear error."""
        db = _make_db(db_type="cassandra")

        result = await dumper._dump_one(db)

        assert result.status == "failed"
        assert "Unsupported" in (result.error or "")
        logger.info("Unsupported DB type correctly reported: %s", result.error)


class TestCredentialRedactionInDumper:
    """Test that credentials are redacted from dump error messages."""

    async def test_dump_all_outer_catch_redacts_credentials(self, dumper):
        """When dump_all's outer catch handles an exception, error is redacted."""
        db = _make_db(db_type="postgres")

        # Make _dump_one itself raise (bypassing the inner try/except in
        # _dump_postgres_blocking) so that dump_all's outer catch redacts.
        async def _raising_dump_one(db_obj):
            raise RuntimeError("Connection failed with password=SuperSecret123")

        dumper._dump_one = _raising_dump_one

        results = await dumper.dump_all([db])

        assert len(results) == 1
        assert results[0].status == "failed"
        assert "SuperSecret123" not in (results[0].error or "")
        assert "[REDACTED]" in (results[0].error or "")
        logger.info("Credentials redacted from dump error: %s", results[0].error)

    def test_postgres_internal_catch_includes_error(self, dumper):
        """_dump_postgres_blocking internal catch preserves the error message."""
        db = _make_db()
        dumper.docker.containers.get = MagicMock(
            side_effect=Exception("No such container: test-pg")
        )

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert "No such container" in (result.error or "")
        logger.info("Internal catch preserves error: %s", result.error)


class TestSQLiteSpecific:
    """Test SQLite-specific dump edge cases."""

    async def test_sqlite_no_host_path_returns_failed(self, dumper):
        """SQLite dump without host_path should fail with clear error."""
        db = _make_db(db_type="sqlite", host_path=None)

        result = await dumper._dump_one(db)

        assert result.status == "failed"
        assert "host path" in (result.error or "").lower()
        logger.info("SQLite no host_path correctly reported: %s", result.error)

    async def test_sqlite_path_traversal_rejected(self, dumper):
        """SQLite dump with path traversal should be rejected."""
        db = _make_db(db_type="sqlite", host_path="/mnt/user/../../etc/passwd")

        result = await dumper._dump_one(db)

        assert result.status == "failed"
        assert "traversal" in (result.error or "").lower() or "Invalid" in (result.error or "")
        logger.info("SQLite path traversal correctly rejected: %s", result.error)
