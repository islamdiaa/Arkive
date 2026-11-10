"""Regression tests for problems the real-engine restore round trip found
(scripts/restore-roundtrip/restore_roundtrip.py: Postgres 16/17, MariaDB 11.4,
MySQL 8.4, SQLite in WAL mode, dumped -> backed up -> restored -> loaded).
"""

import importlib.util
import os
import shlex
import shutil
import stat
import subprocess  # nosec B404 - runs the generated restore commands against stub binaries
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services.db_dumper import (
    MYSQL_DUMP_OPTIONS,
    DBDumper,
    mysql_credentials_from_env,
)
from app.services.discovery import MYSQL_LIST_DATABASES_SQL, DiscoveryEngine
from app.services.restore_plan import RESTORE_PLAN_TEMPLATE, RestorePlanGenerator
from app.utils.redact import redact_credentials

MYSQL_OK = b"-- MariaDB dump 10.19\nCREATE TABLE `t` (`id` int);\n-- Dump completed on 2026-11-09 21:26:02\n"


def _mysql_container(tags=(), config_image=None, env=(), probe=None):
    calls = []

    def exec_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:] == ["--version"]:
            return SimpleNamespace(exit_code=(probe or {}).get(cmd[0], 127), output=b"")
        return 0, iter([(MYSQL_OK, b"")])

    container = MagicMock()
    container.image.tags = list(tags)
    container.attrs = {"Config": {"Env": list(env), **({"Image": config_image} if config_image else {})}}
    container.exec_run.side_effect = exec_run
    return container, calls


class TestMysqlDumps:
    def test_routines_events_and_utf8mb4_are_dumped(self, tmp_path):
        """mysqldump leaves out procedures, functions and events unless asked;
        older clients default to 3-byte utf8 and turn emoji into "?"."""
        assert {"--routines", "--events", "--default-character-set=utf8mb4"} <= set(MYSQL_DUMP_OPTIONS)
        container, calls = _mysql_container(tags=["mysql:8.4"], env=["MYSQL_ROOT_PASSWORD=pw"])
        docker = MagicMock()
        docker.containers.get.return_value = container
        dumper = DBDumper(docker, SimpleNamespace(dump_dir=tmp_path))
        db = DiscoveredDatabase(container_name="db", db_type="mariadb", db_name="Photo Library")
        result = dumper._dump_mariadb_blocking(db)
        assert result.status == "success", result.error
        cmd = calls[-1]
        assert cmd[0] == "mysqldump"
        assert cmd[cmd.index("--databases") + 1] == "Photo Library"  # names listed by discovery may have spaces
        for option in ("--routines", "--events", "--default-character-set=utf8mb4", "--single-transaction"):
            assert option in cmd

    @pytest.mark.parametrize(
        ("tags", "config_image", "probe", "expected"),
        [
            (["mariadb:11.4"], None, None, "mariadb-dump"),
            (["lscr.io/linuxserver/mariadb:latest"], None, None, "mariadb-dump"),
            (["mysql:8.4"], None, None, "mysqldump"),
            # Image lost its tag after a newer pull: the container's image reference decides.
            ([], "mariadb:11.4", None, "mariadb-dump"),
            ([], "mysql:8.0", None, "mysqldump"),
            # Unrecognisable name: probe the binaries.
            ([], "registry.local/db@sha256:abc", {"mariadb-dump": 0}, "mariadb-dump"),
            ([], "registry.local/db@sha256:abc", {"mysqldump": 0}, "mysqldump"),
        ],
    )
    def test_dump_binary_matches_the_image(self, tags, config_image, probe, expected):
        container, _calls = _mysql_container(tags=tags, config_image=config_image, probe=probe)
        assert DBDumper._mysql_dump_binary(container) == expected

    @pytest.mark.parametrize("name", ["-x", "", "a\\b", "bad\nname", "tab\tname", "x" * 65])
    def test_unsafe_database_names_are_refused(self, name):
        with pytest.raises(ValueError):
            DBDumper._sanitize_mysql_dbname(name)

    @pytest.mark.parametrize("name", ["example.com", "blog.example.com", "a/b", "my db", "café", "x" * 64])
    def test_legal_database_names_are_accepted(self, name):
        """MySQL/MariaDB allow dots and slashes in quoted database names (on
        disk "example.com" is the folder "example@002ecom"); checked against
        MariaDB 11.4, where mariadb-dump --databases example.com and a/b both
        succeed. The name is one argv element, so neither is an injection risk."""
        assert DBDumper._sanitize_mysql_dbname(name) == name

    def test_dotted_database_name_is_dumped(self, tmp_path):
        container, calls = _mysql_container(tags=["mariadb:11.4"], env=["MARIADB_ROOT_PASSWORD=pw"])
        docker = MagicMock()
        docker.containers.get.return_value = container
        dumper = DBDumper(docker, SimpleNamespace(dump_dir=tmp_path))
        db = DiscoveredDatabase(container_name="db", db_type="mariadb", db_name="example.com")
        result = dumper._dump_mariadb_blocking(db)
        assert result.status == "success", result.error
        cmd = calls[-1]
        assert cmd[cmd.index("--databases") + 1] == "example.com"

    def test_credentials(self):
        assert mysql_credentials_from_env({"MARIADB_ROOT_PASSWORD": "r", "MARIADB_USER": "a"}) == ("root", "r")
        assert mysql_credentials_from_env({"MYSQL_USER": "app", "MYSQL_PASSWORD": "p"}) == ("app", "p")
        assert mysql_credentials_from_env({}) == ("root", "")


def _db_container(name, tags, env, status="running", handler=None):
    container = MagicMock()
    container.name = name
    container.status = status
    container.image.tags = list(tags)
    container.labels = {}
    container.attrs = {"Config": {"Env": list(env), "Labels": {}}, "Mounts": []}
    container.exec_run.side_effect = handler or (lambda cmd, **kw: (1, (b"", b"no")))
    return container


@pytest.fixture(params=["profile", "auto-detect"])
def discovery_config(request, mock_config):
    """Both discovery paths: the mariadb.yaml profile and image auto-detection."""
    if request.param == "profile":
        mock_config.profiles_dir = Path(__file__).resolve().parents[3] / "profiles"
        assert (mock_config.profiles_dir / "mariadb.yaml").is_file()
    else:
        mock_config.profiles_dir = mock_config.config_dir / "no-profiles"
    return mock_config


class TestMysqlDatabaseEnumeration:
    """A shared MariaDB/MySQL server holds several apps' databases; only the
    one named by MYSQL_DATABASE used to be dumped."""

    @pytest.mark.asyncio
    async def test_every_application_database_is_discovered(self, discovery_config):
        seen = []

        def handler(cmd, **kwargs):
            seen.append((list(cmd), kwargs.get("environment")))
            assert cmd[-1] == MYSQL_LIST_DATABASES_SQL
            return 0, (b"appdb\nnextcloud\nphotos\n", b"")

        maria = _db_container(
            "mariadb", ["mariadb:11.4"], ["MARIADB_DATABASE=appdb", "MARIADB_ROOT_PASSWORD=pw"], handler=handler
        )
        docker = MagicMock()
        docker.containers.list.return_value = [maria]
        found = await DiscoveryEngine(docker, discovery_config).scan()
        assert [d.db_name for d in found[0].databases] == ["appdb", "nextcloud", "photos"]
        assert all(d.db_type == "mariadb" for d in found[0].databases)
        assert found[0].warnings == []
        cmd, env = seen[0]
        assert cmd[:3] == ["mariadb", "-u", "root"] and env == {"MYSQL_PWD": "pw"}
        assert "pw" not in " ".join(cmd)

    @pytest.mark.asyncio
    async def test_falls_back_to_the_mysql_client(self, discovery_config):
        def handler(cmd, **kwargs):
            if cmd[0] == "mariadb":
                return 127, (None, b'OCI runtime exec failed: exec: "mariadb": executable file not found in $PATH')
            return 0, (b"wiki\n", b"")

        mysql = _db_container(
            "mysql", ["mysql:8.4"], ["MYSQL_DATABASE=appdb", "MYSQL_ROOT_PASSWORD=pw"], handler=handler
        )
        docker = MagicMock()
        docker.containers.list.return_value = [mysql]
        found = await DiscoveryEngine(docker, discovery_config).scan()
        assert [d.db_name for d in found[0].databases] == ["appdb", "wiki"]

    @pytest.mark.asyncio
    async def test_listing_failure_is_a_coverage_warning(self, discovery_config):
        def handler(cmd, **kwargs):
            return 1, (b"", b"ERROR 1045 (28000): Access denied for user 'root'@'localhost'")

        maria = _db_container("mariadb", ["mariadb:11.4"], ["MARIADB_DATABASE=appdb"], handler=handler)
        docker = MagicMock()
        docker.containers.list.return_value = [maria]
        found = await DiscoveryEngine(docker, discovery_config).scan()
        assert [d.db_name for d in found[0].databases] == ["appdb"]
        assert found[0].warnings and "Access denied" in found[0].warnings[0]

    @pytest.mark.asyncio
    async def test_server_without_application_databases_is_not_a_failure(self, discovery_config):
        """`mariadb -N -B` prints nothing for zero rows; docker-py's demuxed
        stdout is then None. That is an empty list, not a listing failure."""

        def handler(cmd, **kwargs):
            return 0, (None, None)

        maria = _db_container("mariadb", ["mariadb:11.4"], ["MARIADB_ROOT_PASSWORD=pw"], handler=handler)
        docker = MagicMock()
        docker.containers.list.return_value = [maria]
        found = await DiscoveryEngine(docker, discovery_config).scan()
        assert [d.db_name for d in found[0].databases] == ["mysql"]
        assert found[0].warnings == []

    @pytest.mark.asyncio
    async def test_stopped_server_is_not_queried(self, discovery_config):
        maria = _db_container("mariadb", ["mariadb:11.4"], ["MARIADB_DATABASE=appdb"], status="exited")
        docker = MagicMock()
        docker.containers.list.return_value = [maria]
        found = await DiscoveryEngine(docker, discovery_config).scan()
        assert [d.db_name for d in found[0].databases] == ["appdb"]
        maria.exec_run.assert_not_called()


# ---------------------------------------------------------------------------
# Restore plan commands, executed against stub binaries
# ---------------------------------------------------------------------------


def _stub_bin(tmp_path, *names):
    """A PATH directory with ``cat``, a ``docker`` that runs ``docker exec -i
    CONTAINER sh -c SCRIPT`` locally, and client stubs that print their
    arguments and the dump they were fed. Nothing else is on PATH, so a real
    psql/mysql/mariadb on the machine running the tests is never used."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    os.symlink(shutil.which("cat"), bindir / "cat")
    scripts = {"docker": '#!/bin/sh\n[ "$1 $2" = "exec -i" ] || exit 64\nshift 4\nexec /bin/sh "$@"\n'}
    scripts.update({name: f'#!/bin/sh\necho "{name} $*"\ncat\n' for name in names})
    for name, body in scripts.items():
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run_plain_restore(full: str, tmp_path, bindir, env: dict) -> list[str]:
    """Run the plan's plain-dump command with POSIX sh from a directory that
    holds one dump matching its glob; returns the client's output lines."""
    lines = full.splitlines()
    command = lines[lines.index("# plain dump (.sql):") + 1]
    glob = shlex.split(command)[1]
    workdir = tmp_path / "restored-dumps"
    workdir.mkdir(exist_ok=True)
    for old in workdir.iterdir():
        old.unlink()
    (workdir / glob.replace("[0-9]*", "20261109_212602")).write_text("-- the dump\n")
    result = subprocess.run(  # nosec B603 - fixed argv, stub binaries
        ["/bin/sh", "-c", command],
        cwd=workdir,
        env={"PATH": str(bindir), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


class TestRestorePlanCommands:
    def test_postgres_commands_use_the_containers_superuser(self, tmp_path):
        """POSTGRES_USER is often not "postgres" (Immich-style setups), and then
        `psql -U postgres` fails with 'role "postgres" does not exist'."""
        gen = RestorePlanGenerator(SimpleNamespace())
        bindir = _stub_bin(tmp_path, "psql")
        for _short, full in (
            gen._get_restore_commands("postgres", "appdb", "immich_postgres"),
            gen._get_globals_restore_commands("immich_postgres"),
        ):
            out = _run_plain_restore(full, tmp_path, bindir, {"POSTGRES_USER": "immich"})
            assert out == ["psql -U immich -d postgres", "-- the dump"]
            out = _run_plain_restore(full, tmp_path, bindir, {})
            assert out == ["psql -U postgres -d postgres", "-- the dump"]

    @pytest.mark.parametrize(
        ("binaries", "env", "expected"),
        [
            (("mariadb",), {"MARIADB_ROOT_PASSWORD": "s3cret"}, "mariadb -uroot -ps3cret"),
            (("mysql",), {"MYSQL_ROOT_PASSWORD": "s3cret"}, "mysql -uroot -ps3cret"),
            (("mariadb", "mysql"), {"MYSQL_ROOT_PASSWORD": "s3cret"}, "mariadb -uroot -ps3cret"),
            # No root password (MYSQL_ALLOW_EMPTY_PASSWORD, the dumper's
            # root-without-password fallback): no bare -p, which would make
            # the client read the dump's first line as the password.
            (("mariadb",), {}, "mariadb -uroot"),
            (("mysql",), {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_ROOT_PASSWORD": ""}, "mysql -uroot"),
        ],
    )
    def test_mysql_command_uses_the_servers_client_and_root_password(self, tmp_path, binaries, env, expected):
        """MariaDB 11+ images have no `mysql` binary, and root needs its password."""
        _short, full = RestorePlanGenerator(SimpleNamespace())._get_restore_commands("mariadb", "appdb", "maria")
        out = _run_plain_restore(full, tmp_path, _stub_bin(tmp_path, *binaries), env)
        assert out == [expected, "-- the dump"]

    def test_mysql_root_password_is_one_argument(self, tmp_path):
        """A root password with spaces or quotes reaches the client as one -p argument."""
        bindir = _stub_bin(tmp_path)
        stub = bindir / "mariadb"
        stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "arg:$a"; done\ncat\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        _short, full = RestorePlanGenerator(SimpleNamespace())._get_restore_commands("mariadb", "appdb", "maria")
        out = _run_plain_restore(full, tmp_path, bindir, {"MARIADB_ROOT_PASSWORD": "p a$s\"w'x"})
        assert out == ["arg:-uroot", "arg:-pp a$s\"w'x", "-- the dump"]

    def test_sqlite_restore_targets_the_real_file_and_drops_stale_wal(self, tmp_path):
        """The old plan copied to a guessed /mnt/user/appdata/<container>/data/
        path and left the old -wal file, which SQLite replays over the
        restored database (the restore silently has no effect)."""
        host_path = "/mnt/user/appdata/my app/db.sqlite3"
        _short, full = RestorePlanGenerator(SimpleNamespace())._get_restore_commands(
            "sqlite", "db.sqlite3", "vaultwarden", host_path
        )
        lines = [line for line in full.splitlines() if not line.startswith("#")]
        assert lines[0] == "docker stop vaultwarden" and lines[-1] == "docker start vaultwarden"
        rm = shlex.split(lines[1])
        assert rm[:2] == ["rm", "-f"] and {f"{host_path}-wal", f"{host_path}-shm"} <= set(rm)
        cp = shlex.split(lines[2])
        assert cp[0] == "cp" and cp[-1] == host_path and cp[1].startswith("vaultwarden_db.sqlite3-")
        assert "/data/" not in full

    def test_restic_steps_can_prompt_for_the_password(self):
        """`docker exec arkive restic ...` without a TTY fails with "an empty
        password is not allowed"; restic needs -it to ask for it."""
        assert "docker exec arkive restic" not in RESTORE_PLAN_TEMPLATE
        assert RESTORE_PLAN_TEMPLATE.count("docker exec -it arkive restic") == 2


class TestRedactionKeepsNames:
    """Drill/run messages name containers like "immich-postgres"; the -pPASSWORD
    rule used to turn them into "immich-p[REDACTED]"."""

    @pytest.mark.parametrize(
        "text",
        [
            "1 database(s) did not restore: immich-postgres/immich",
            "paperless-pg/paperless: only 3 of 4 tables were restored",
            "arkive-rt-pg16/appdb, arkive-rt-pg17/appdb",
        ],
    )
    def test_container_names_survive(self, text):
        assert redact_credentials(text) == text

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("mysqldump -pS3cret -uroot", "S3cret"),
            ('cmd "-pS3cret"', "S3cret"),
            ("args=-pS3cret", "S3cret"),
            ("-pS3cret at start", "S3cret"),
        ],
    )
    def test_password_flags_are_still_redacted(self, text, secret):
        assert secret not in redact_credentials(text)


HARNESS_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "restore-roundtrip" / "restore_roundtrip.py"


@pytest.fixture
def harness_module():
    """The round-trip harness script, imported as a module (stdlib only)."""
    if not HARNESS_SCRIPT.is_file():
        pytest.skip("scripts/restore-roundtrip is not part of this checkout")
    name = "arkive_restore_roundtrip_harness"
    spec = importlib.util.spec_from_file_location(name, HARNESS_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


class TestRoundTripHarnessWorkdir:
    """Teardown deletes the workdir's contents as root; it must only ever
    delete a directory the harness marked as its own."""

    @staticmethod
    def _harness(module, workdir, prefix="arkive-rt"):
        harness = module.Harness(SimpleNamespace(workdir=str(workdir), prefix=prefix, port=8320))
        calls = []
        harness.host = lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0)
        return harness, calls

    def test_non_empty_foreign_directory_is_refused_and_kept(self, harness_module, tmp_path):
        workdir = tmp_path / "scratch"
        workdir.mkdir()
        (workdir / "precious.txt").write_text("keep me")
        harness, calls = self._harness(harness_module, workdir)
        with pytest.raises(RuntimeError, match="refusing to use --workdir"):
            harness.claim_workdir()
        harness.down()
        assert (workdir / "precious.txt").read_text() == "keep me"
        assert not (workdir / harness_module.WORKDIR_MARKER).exists()
        assert all(call[0] != "run" for call in calls)  # no root rm -rf container

    def test_marker_of_another_prefix_is_not_ownership(self, harness_module, tmp_path):
        workdir = tmp_path / "state"
        other, _ = self._harness(harness_module, workdir, prefix="other-rt")
        other.claim_workdir()
        (workdir / "data").write_text("x")
        harness, calls = self._harness(harness_module, workdir)
        with pytest.raises(RuntimeError):
            harness.claim_workdir()
        harness.down()
        assert (workdir / "data").exists() and all(call[0] != "run" for call in calls)

    def test_own_workdir_is_claimed_reused_and_removed(self, harness_module, tmp_path):
        workdir = tmp_path / "new" / "state"
        harness, calls = self._harness(harness_module, workdir)
        harness.claim_workdir()
        assert harness.owns_workdir()
        (workdir / "docker").mkdir()
        harness.claim_workdir()  # a later run (or --reuse) on the same marked directory
        harness.down()
        assert not workdir.exists()
        assert any(call[0] == "run" for call in calls)

    def test_aborted_run_leaves_a_foreign_workdir_alone(self, harness_module, tmp_path, monkeypatch):
        """The reported scenario: an existing directory with files plus a
        mistyped image. The run aborts and teardown must not empty the directory."""
        workdir = tmp_path / "scratch"
        workdir.mkdir()
        (workdir / "precious.txt").write_text("keep me")
        calls = []
        monkeypatch.setattr(
            harness_module.Harness,
            "host",
            staticmethod(lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=1)),
        )
        monkeypatch.setattr(harness_module.shutil, "which", lambda name: "/usr/bin/docker")
        monkeypatch.setattr(
            sys, "argv", ["restore_roundtrip.py", "--workdir", str(workdir), "--image", "arkive-rt:typo"]
        )
        assert harness_module.main() == 2
        assert (workdir / "precious.txt").read_text() == "keep me"
        assert all(call[0] != "run" for call in calls)
