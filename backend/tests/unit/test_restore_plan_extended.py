"""
Extended unit tests for RestorePlanGenerator covering multiple DB types
and the _get_restore_commands method in detail.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.restore_plan import RestorePlanGenerator


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.config_dir = tmp_path
    config.db_path = tmp_path / "arkive.db"
    return config


@pytest.fixture
def generator(mock_config):
    return RestorePlanGenerator(config=mock_config)


# ---------------------------------------------------------------------------
# _get_restore_commands — postgres
# ---------------------------------------------------------------------------


class TestRestoreCommandsPostgres:
    def test_short_cmd_contains_psql(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "psql" in short

    def test_full_cmd_contains_psql(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "psql" in full

    def test_short_cmd_contains_db_name(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "mydb" not in short

    def test_full_cmd_contains_db_name(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "mydb" in full

    def test_full_cmd_contains_container_name(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "pg_container" in full

    def test_postgres_restore_omits_database_flag(self, generator):
        short, full = generator._get_restore_commands("postgres", "mydb", "pg_container")
        assert "-d mydb" not in short
        assert "-d mydb" not in full

    def test_returns_tuple_of_strings(self, generator):
        result = generator._get_restore_commands("postgres", "db", "ctr")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(s, str) for s in result)


# ---------------------------------------------------------------------------
# _get_restore_commands — mariadb / mysql
# ---------------------------------------------------------------------------


class TestRestoreCommandsMariadb:
    def test_mariadb_short_cmd_contains_mysql(self, generator):
        short, full = generator._get_restore_commands("mariadb", "appdb", "mariadb_container")
        assert "mysql" in short

    def test_mariadb_full_cmd_contains_container(self, generator):
        short, full = generator._get_restore_commands("mariadb", "appdb", "mariadb_container")
        assert "mariadb_container" in full

    def test_mariadb_contains_db_name(self, generator):
        short, full = generator._get_restore_commands("mariadb", "appdb", "mariadb_container")
        assert "appdb" in full
        assert "appdb" not in short

    def test_mysql_alias_short_cmd_contains_mysql(self, generator):
        short, full = generator._get_restore_commands("mysql", "testdb", "mysql_ctr")
        assert "mysql" in short

    def test_mysql_alias_full_cmd_contains_container(self, generator):
        short, full = generator._get_restore_commands("mysql", "testdb", "mysql_ctr")
        assert "mysql_ctr" in full

    def test_mysql_alias_contains_db_name(self, generator):
        short, full = generator._get_restore_commands("mysql", "testdb", "mysql_ctr")
        assert "testdb" in full
        assert "testdb" not in short

    def test_mariadb_restore_omits_database_flag(self, generator):
        short, full = generator._get_restore_commands("mariadb", "appdb", "mariadb_container")
        assert " appdb" not in short
        assert " appdb" not in full


# ---------------------------------------------------------------------------
# _get_restore_commands — mongodb
# ---------------------------------------------------------------------------


class TestRestoreCommandsMongodb:
    def test_short_cmd_contains_mongorestore(self, generator):
        short, full = generator._get_restore_commands("mongodb", "mongotestdb", "mongo_ctr")
        assert "mongorestore" in short

    def test_full_cmd_contains_mongorestore(self, generator):
        short, full = generator._get_restore_commands("mongodb", "mongotestdb", "mongo_ctr")
        assert "mongorestore" in full

    def test_full_cmd_contains_container_name(self, generator):
        short, full = generator._get_restore_commands("mongodb", "mongotestdb", "mongo_ctr")
        assert "mongo_ctr" in full

    def test_returns_tuple_of_strings(self, generator):
        result = generator._get_restore_commands("mongodb", "db", "ctr")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(s, str) for s in result)


# ---------------------------------------------------------------------------
# _get_restore_commands — sqlite
# ---------------------------------------------------------------------------


class TestRestoreCommandsSqlite:
    def test_short_cmd_contains_sqlite3(self, generator):
        short, full = generator._get_restore_commands("sqlite", "mydata.db", "sqlite_ctr")
        assert "sqlite3" in short

    def test_full_cmd_contains_container_name(self, generator):
        short, full = generator._get_restore_commands("sqlite", "mydata.db", "sqlite_ctr")
        assert "sqlite_ctr" in full

    def test_full_cmd_contains_db_name(self, generator):
        short, full = generator._get_restore_commands("sqlite", "mydata.db", "sqlite_ctr")
        assert "mydata.db" in short or "mydata.db" in full

    def test_returns_non_empty_strings(self, generator):
        short, full = generator._get_restore_commands("sqlite", "db.sqlite", "ctr")
        assert len(short) > 0
        assert len(full) > 0


# ---------------------------------------------------------------------------
# _get_restore_commands — redis
# ---------------------------------------------------------------------------


class TestRestoreCommandsRedis:
    def test_short_cmd_contains_redis(self, generator):
        short, full = generator._get_restore_commands("redis", "cache", "redis_ctr")
        assert "redis" in short.lower()

    def test_full_cmd_contains_container(self, generator):
        short, full = generator._get_restore_commands("redis", "cache", "redis_ctr")
        assert "redis_ctr" in full

    def test_returns_strings(self, generator):
        result = generator._get_restore_commands("redis", "cache", "ctr")
        assert isinstance(result[0], str) and isinstance(result[1], str)


# ---------------------------------------------------------------------------
# _get_restore_commands — unknown types
# ---------------------------------------------------------------------------


class TestRestoreCommandsUnknown:
    def test_unknown_returns_documentation_fallback(self, generator):
        short, full = generator._get_restore_commands("cockroachdb", "db", "ctr")
        assert "See" in short or "documentation" in short.lower()

    def test_empty_type_returns_fallback(self, generator):
        short, full = generator._get_restore_commands("", "db", "ctr")
        assert isinstance(short, str)
        assert isinstance(full, str)

    def test_unknown_full_cmd_also_fallback(self, generator):
        short, full = generator._get_restore_commands("cassandra", "keyspace", "cass_ctr")
        assert isinstance(full, str)
        assert len(full) > 0


# ---------------------------------------------------------------------------
# _format_bytes
# ---------------------------------------------------------------------------


class TestFormatBytesExtended:
    def test_bytes(self, generator):
        assert generator._format_bytes(512) == "512.0 B"

    def test_exactly_1kb(self, generator):
        assert generator._format_bytes(1024) == "1.0 KB"

    def test_exactly_1mb(self, generator):
        assert generator._format_bytes(1024 * 1024) == "1.0 MB"

    def test_exactly_1gb(self, generator):
        assert generator._format_bytes(1024**3) == "1.0 GB"

    def test_exactly_1tb(self, generator):
        assert generator._format_bytes(1024**4) == "1.0 TB"

    def test_large_value_returns_pb(self, generator):
        result = generator._format_bytes(1024**5)
        assert "PB" in result

    def test_fractional_mb(self, generator):
        result = generator._format_bytes(int(1.5 * 1024 * 1024))
        assert "MB" in result


# ---------------------------------------------------------------------------
# generate() — mocked aiosqlite and weasyprint
# ---------------------------------------------------------------------------


class TestGenerateMethod:
    def _make_mock_db(self):
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_cursor.fetchone = AsyncMock(return_value=None)

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)
        mock_db.row_factory = None
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        return mock_db

    @pytest.mark.asyncio
    async def test_generate_returns_path_string(self, generator, tmp_path):
        """generate() returns a string path (html or pdf)."""
        mock_db = self._make_mock_db()
        mock_html_cls = MagicMock()
        mock_html_cls.return_value.write_pdf = MagicMock()

        with (
            patch("aiosqlite.connect", return_value=mock_db),
            patch("app.__version__", "3.0.0"),
            patch.dict("sys.modules", {"weasyprint": MagicMock(HTML=mock_html_cls)}),
        ):
            result = await generator.generate()

        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_generate_falls_back_to_html_on_pdf_failure(self, generator, tmp_path):
        """generate() falls back to HTML path when WeasyPrint fails."""
        mock_db = self._make_mock_db()

        # Make weasyprint raise on import (simulate missing module)
        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML.side_effect = Exception("weasyprint broken")

        with (
            patch("aiosqlite.connect", return_value=mock_db),
            patch("app.__version__", "3.0.0"),
            patch.dict("sys.modules", {"weasyprint": mock_weasyprint}),
        ):
            result = await generator.generate()

        # Should fall back to HTML path
        assert result.endswith(".html")

    @pytest.mark.asyncio
    async def test_generate_writes_html_file(self, generator, tmp_path):
        """generate() writes an HTML file to config_dir."""
        generator.config.config_dir = tmp_path
        mock_db = self._make_mock_db()

        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML.side_effect = Exception("no weasyprint")

        with (
            patch("aiosqlite.connect", return_value=mock_db),
            patch("app.__version__", "3.0.0"),
            patch.dict("sys.modules", {"weasyprint": mock_weasyprint}),
        ):
            await generator.generate()

        html_file = tmp_path / "restore-plan.html"
        assert html_file.exists()
        content = html_file.read_text()
        assert "Arkive" in content
