"""Tests for exit code capture and SQLite injection fixes in db_dumper.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_db(container_name="testcontainer", db_type="postgres", db_name="testdb", host_path=None):
    """Create a minimal DiscoveredDatabase mock."""
    from app.models.discovery import DiscoveredDatabase

    return DiscoveredDatabase(
        container_name=container_name,
        db_type=db_type,
        db_name=db_name,
        host_path=host_path,
    )


def _make_dumper(tmp_path):
    """Create a DBDumper with mocked docker client and tmp dump dir."""
    from app.core.config import ArkiveConfig
    from app.services.db_dumper import DBDumper

    config = MagicMock(spec=ArkiveConfig)
    config.dump_dir = tmp_path
    docker_client = MagicMock()
    return DBDumper(docker_client, config)


def _streaming_exec_run(exit_code: int, stdout_chunks: list[bytes], stderr_chunks: list[bytes]):
    """Build the (exit_code, generator) tuple that docker-py returns for stream=True, demux=True."""

    def _gen():
        for s, e in zip(stdout_chunks, stderr_chunks):
            yield (s, e)
        # Yield any remaining chunks
        for s in stdout_chunks[len(stderr_chunks) :]:
            yield (s, None)
        for e in stderr_chunks[len(stdout_chunks) :]:
            yield (None, e)

    return exit_code, _gen()


# ---------------------------------------------------------------------------
# Task A — exit code tests
# ---------------------------------------------------------------------------


class TestPostgresExitCode:
    """pg_dump non-zero exit code must produce failure status."""

    def test_nonzero_exit_returns_failed(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="postgres", db_name="mydb")

        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        # Simulate pg_dump failing with exit code 1 and some stderr
        stderr_msg = b"pg_dump: error: connection to server failed"
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=1,
            stdout_chunks=[],
            stderr_chunks=[stderr_msg],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert "1" in result.error  # exit code in message
        assert result.dump_size_bytes == 0

    def test_zero_exit_with_data_returns_success(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="postgres", db_name="mydb")

        # Produce enough bytes to pass the >0 check
        fake_sql = b"-- PostgreSQL dump\n" + b"x" * 200
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=0,
            stdout_chunks=[fake_sql],
            stderr_chunks=[b""],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0

    def test_none_exit_code_treated_as_success(self, tmp_path):
        """exit_code=None (docker-py streaming behavior) must not be treated as failure."""
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="postgres", db_name="mydb")

        fake_sql = b"-- PostgreSQL dump\n" + b"x" * 200
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=None,
            stdout_chunks=[fake_sql],
            stderr_chunks=[b""],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "success"
        assert result.dump_size_bytes > 0

    def test_nonzero_exit_error_includes_stderr(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="postgres", db_name="mydb")

        stderr_msg = b"FATAL: password authentication failed"
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=postgres"]}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=2,
            stdout_chunks=[],
            stderr_chunks=[stderr_msg],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "failed"
        assert "password authentication failed" in result.error


class TestMariaDBExitCode:
    """mysqldump non-zero exit code must produce failure status."""

    def test_nonzero_exit_returns_failed(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mariadb", db_name="wordpress")

        stderr_msg = b"mysqldump: Got error: 1045: Access denied"
        container = MagicMock()
        container.attrs = {
            "Config": {
                "Env": [
                    "MYSQL_ROOT_PASSWORD=secret",
                ]
            }
        }
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=1,
            stdout_chunks=[],
            stderr_chunks=[stderr_msg],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "failed"
        assert "1" in result.error

    def test_zero_exit_with_data_returns_success(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mariadb", db_name="wordpress")

        fake_sql = b"-- MariaDB dump\n" + b"y" * 200
        container = MagicMock()
        container.attrs = {
            "Config": {
                "Env": [
                    "MYSQL_ROOT_PASSWORD=secret",
                ]
            }
        }
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=0,
            stdout_chunks=[fake_sql],
            stderr_chunks=[b""],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "success"

    def test_nonzero_exit_error_includes_exit_code(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mariadb", db_name="wordpress")

        container = MagicMock()
        container.attrs = {"Config": {"Env": ["MYSQL_ROOT_PASSWORD=secret"]}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=7,
            stdout_chunks=[],
            stderr_chunks=[b"some error"],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "failed"
        assert "7" in result.error


class TestMongoDBExitCode:
    """mongodump non-zero exit code must produce failure status."""

    def test_nonzero_exit_returns_failed(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mongodb", db_name="admin")

        stderr_msg = b"Failed: error connecting to db server: no reachable servers"
        container = MagicMock()
        container.attrs = {"Config": {"Env": []}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=1,
            stdout_chunks=[],
            stderr_chunks=[stderr_msg],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mongodb_blocking(db)

        assert result.status == "failed"
        assert "1" in result.error

    def test_zero_exit_with_data_returns_success(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mongodb", db_name="admin")

        # mongodump archive data
        fake_archive = b"\x89MNG" + b"z" * 200
        container = MagicMock()
        container.attrs = {"Config": {"Env": []}}
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=0,
            stdout_chunks=[fake_archive],
            stderr_chunks=[b"2024-01-01T00:00:00.000+0000\t writing admin.users to archive"],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mongodb_blocking(db)

        assert result.status == "success"

    def test_nonzero_exit_error_includes_stderr_text(self, tmp_path):
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="mongodb", db_name="admin")

        stderr_msg = b"authentication failed for user admin"
        container = MagicMock()
        container.attrs = {
            "Config": {
                "Env": [
                    "MONGO_INITDB_ROOT_USERNAME=admin",
                    "MONGO_INITDB_ROOT_PASSWORD=pass",
                ]
            }
        }
        container.exec_run.return_value = _streaming_exec_run(
            exit_code=1,
            stdout_chunks=[],
            stderr_chunks=[stderr_msg],
        )
        dumper.docker.containers.get.return_value = container

        result = dumper._dump_mongodb_blocking(db)

        assert result.status == "failed"
        assert "authentication failed" in result.error


# ---------------------------------------------------------------------------
# Task B — SQLite injection tests
# ---------------------------------------------------------------------------


class TestSQLiteInjection:
    """SQLite backup path must be shell-safe."""

    @pytest.mark.asyncio
    async def test_normal_path_succeeds(self, tmp_path):
        """A normal host_path produces a successful backup."""
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="sqlite", db_name="db", host_path="/data/app.sqlite3")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_result.stdout = "ok"

        # Create a fake dump file so size check passes
        async def fake_run(cmd, **kwargs):
            # Write a fake file at the dump path (3rd element is ".backup 'path'")
            dump_arg = cmd[2]
            # Extract path from .backup 'path'
            import shlex

            path = shlex.split(dump_arg.replace(".backup ", ""))[0]
            Path(path).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
            return mock_result

        with patch("app.services.db_dumper.run_command", side_effect=fake_run):
            result = await dumper._dump_sqlite(db)

        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_readonly_sqlite_retries_with_immutable_uri(self, tmp_path):
        """Read-only mounted SQLite files should retry with immutable URI mode."""
        import shlex

        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="sqlite", db_name="logs.db", host_path="/data/logs.db")

        first = MagicMock()
        first.returncode = 0
        first.stderr = "Error: unable to open database file\n"
        first.stdout = ""

        second = MagicMock()
        second.returncode = 0
        second.stderr = ""
        second.stdout = ""

        calls = []

        async def fake_run(cmd, **kwargs):
            calls.append(cmd)
            backup_arg = cmd[2]
            path = shlex.split(backup_arg.replace(".backup ", ""))[0]
            if len(calls) == 2:
                Path(path).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
                return second
            return first

        with patch("app.services.db_dumper.run_command", side_effect=fake_run):
            result = await dumper._dump_sqlite(db)

        assert result.status == "success"
        assert calls[0][1] == "/data/logs.db"
        assert calls[1][1] == "file:/data/logs.db?mode=ro&immutable=1"

    @pytest.mark.asyncio
    async def test_path_with_spaces_is_quoted(self, tmp_path):
        """dump_path with spaces must be quoted in the sqlite3 command."""
        import shlex

        dumper = _make_dumper(tmp_path)
        # Override dump_dir to a path with a space to trigger quoting
        space_dir = tmp_path / "my dumps"
        space_dir.mkdir()
        dumper.dump_dir = space_dir

        db = _make_db(db_type="sqlite", db_name="mydb", host_path="/data/app.sqlite3")

        captured_cmd = []

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_result.stdout = "ok"

        async def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            # Write the dump file
            backup_arg = cmd[2]
            path_part = backup_arg[len(".backup ") :]
            path = shlex.split(path_part)[0]
            Path(path).write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
            return mock_result

        with patch("app.services.db_dumper.run_command", side_effect=fake_run):
            await dumper._dump_sqlite(db)

        # The .backup argument must be properly quoted (contains the space path)
        backup_arg = captured_cmd[2]
        assert backup_arg.startswith(".backup ")
        # shlex.quote wraps paths with spaces in single quotes
        assert "'" in backup_arg or '"' in backup_arg

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tmp_path):
        """host_path with .. traversal must be rejected before any command runs."""
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="sqlite", db_name="db", host_path="/data/../etc/passwd")

        with patch("app.services.db_dumper.run_command") as mock_run:
            result = await dumper._dump_sqlite(db)

        # run_command must never be called
        mock_run.assert_not_called()
        assert result.status == "failed"
        assert "traversal" in result.error.lower() or "invalid" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_host_path_returns_failed(self, tmp_path):
        """Missing host_path must return a clear failure without running any command."""
        dumper = _make_dumper(tmp_path)
        db = _make_db(db_type="sqlite", db_name="db", host_path=None)

        with patch("app.services.db_dumper.run_command") as mock_run:
            result = await dumper._dump_sqlite(db)

        mock_run.assert_not_called()
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# Guard: ensure no _, output patterns slipped back in
# ---------------------------------------------------------------------------


class TestNoDiscardedExitCodes:
    """Regression guard: source file must not contain '_, output =' patterns."""

    def test_no_discarded_exit_codes_in_source(self):
        source_path = Path(__file__).parent.parent.parent / "app" / "services" / "db_dumper.py"
        source = source_path.read_text()
        assert "_, output" not in source, "db_dumper.py still contains '_, output' — exit codes are being discarded!"
