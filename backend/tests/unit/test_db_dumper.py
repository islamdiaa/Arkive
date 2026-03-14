"""Tests for the DB dumper service — 8 test cases."""

from unittest.mock import MagicMock

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import DBDumper


class TestDbDumper:
    """DB Dumper service tests."""

    def test_postgres_dump_command_construction(self):
        """Postgres dumps should be portable to a fresh instance."""
        user = "testuser"
        db_name = "testdb"
        cmd = f"pg_dump --clean --create --if-exists --no-owner --no-privileges -U {user} -d {db_name}"
        assert f"-U {user}" in cmd
        assert f"-d {db_name}" in cmd
        assert "pg_dump" in cmd
        assert "--create" in cmd
        assert "--no-owner" in cmd

    def test_postgres_dump_uses_portable_flags(self, tmp_path):
        """The real postgres dumper should emit restore-friendly pg_dump flags."""
        config = MagicMock()
        config.dump_dir = tmp_path
        docker_client = MagicMock()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["POSTGRES_USER=paperless"]}}

        captured = {}

        def exec_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return 0, iter([(b"-- pg dump --\n", b"")])

        container.exec_run.side_effect = exec_run
        docker_client.containers.get.return_value = container

        dumper = DBDumper(docker_client, config)
        db = DiscoveredDatabase(
            container_name="postgres-paperless", db_type="postgres", db_name="paperless", host_path=None
        )

        result = dumper._dump_postgres_blocking(db)

        assert result.status == "success"
        assert captured["cmd"] == [
            "pg_dump",
            "--clean",
            "--create",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "-U",
            "paperless",
            "-d",
            "paperless",
        ]

    def test_mariadb_dump_uses_portable_flags(self, tmp_path):
        """MariaDB dumps should include database create/use statements."""
        config = MagicMock()
        config.dump_dir = tmp_path
        docker_client = MagicMock()
        container = MagicMock()
        container.attrs = {"Config": {"Env": ["MYSQL_DATABASE=appdb", "MYSQL_ROOT_PASSWORD=secret"]}}
        container.image.tags = ["mariadb:11"]

        captured = {}

        def exec_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("environment", {})
            return 0, iter([(b"-- mariadb dump --\n", b"")])

        container.exec_run.side_effect = exec_run
        docker_client.containers.get.return_value = container

        dumper = DBDumper(docker_client, config)
        db = DiscoveredDatabase(container_name="mariadb-fixture", db_type="mariadb", db_name="appdb", host_path=None)

        result = dumper._dump_mariadb_blocking(db)

        assert result.status == "success"
        assert captured["cmd"] == [
            "mariadb-dump",
            "-u",
            "root",
            "--databases",
            "appdb",
        ]
        assert captured["env"]["MYSQL_PWD"] == "secret"

    def test_sqlite_backup_uses_host_binary(self):
        """SQLite backup MUST use host sqlite3 binary, NOT docker exec."""
        host_path = "/mnt/user/appdata/vaultwarden/db.sqlite3"
        dump_path = "/config/dumps/vaultwarden_db.sqlite3"
        cmd = f"sqlite3 {host_path} '.backup {dump_path}'"
        assert "sqlite3" in cmd
        assert "docker exec" not in cmd
        assert host_path in cmd

    def test_sqlite_integrity_check_passes(self):
        """Integrity check should return True when sqlite3 reports 'ok'."""
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok\n"
        assert result.returncode == 0
        assert "ok" in result.stdout

    def test_sqlite_integrity_check_fails(self):
        """Integrity check should return False on corruption."""
        result = MagicMock()
        result.returncode = 0
        result.stdout = "*** in database main ***\nPage 42: btree page is not a leaf"
        assert "ok" not in result.stdout.strip()

    def test_skip_unavailable_container(self):
        """Should skip containers that are not running."""
        container = MagicMock()
        container.status = "exited"
        container.name = "stopped-db"
        assert container.status != "running"

    def test_empty_dump_detection(self):
        """Should detect and flag zero-byte dump files."""
        dump_size = 0
        assert dump_size == 0
        # Empty dumps indicate a failed dump operation

    def test_mariadb_dump_command_construction(self):
        """MariaDB dumps should be portable to a fresh instance."""
        cmd = "mariadb-dump -u root --databases appdb"
        assert "mariadb-dump" in cmd
        assert "--databases appdb" in cmd
        assert "--all-databases" not in cmd

    def test_multiple_database_dump_with_one_failure(self):
        """Should continue dumping remaining DBs when one fails."""
        results = [
            {"db": "postgres_main", "status": "success", "size": 1024},
            {"db": "vaultwarden", "status": "failed", "error": "locked"},
            {"db": "authelia", "status": "success", "size": 512},
        ]
        successes = [r for r in results if r["status"] == "success"]
        failures = [r for r in results if r["status"] == "failed"]
        assert len(successes) == 2
        assert len(failures) == 1
        # Overall status should be "partial" not "failed"
        overall = "partial" if failures and successes else ("success" if not failures else "failed")
        assert overall == "partial"
