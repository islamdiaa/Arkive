"""MariaDB/MySQL accounts dump (users, roles, grants).

The server doubles answer with the output real servers gave for the same
statements (MariaDB 11.4 and 10.6, MySQL 8.4 and 5.7, captured with the
client in -N -B -r mode); the restore of these files onto fresh servers is
exercised by scripts/restore-roundtrip.
"""

import gzip
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.discovery import DiscoveredDatabase
from app.services import mysql_accounts as accounts_module
from app.services.database_health import check_dump_sizes
from app.services.db_dumper import (
    DUMP_MANIFEST_NAME,
    DBDumper,
    DumpResult,
    cleanup_dump_dir,
    dump_prefix,
    read_dump_manifest,
    server_dump_keys,
    write_dump_manifest,
)
from app.services.mysql_accounts import (
    ACCOUNTS_DB_NAME,
    ACCOUNTS_HEADER,
    ACCOUNTS_KIND,
    Account,
    AccountsDumpError,
    dump_accounts,
    excluded_reason,
)
from app.services.restore_drill import candidates_from_manifest, infer_candidates, select_candidates
from app.services.restore_plan import ACCOUNTS_STEP_LABEL, RestorePlanGenerator
from app.services.verify_engine import VerifyEngine

MARIADB_VERSION = "11.4.13-MariaDB-ubu2404"
MYSQL_VERSION = "8.4.13"


def _hex(value: str) -> str:
    return value.encode().hex().upper()


def _listing(rows: list[tuple[str, str, str]]) -> str:
    return "".join(f"{_hex(user)}\t{_hex(host)}\t{role}\n" for user, host, role in rows)


MARIADB_ACCOUNTS = [
    ("PUBLIC", "", "Y"),
    ("alice", "%", "N"),
    ("app", "%", "N"),
    ("bob", "192.168.1.%", "N"),
    ("healthcheck", "127.0.0.1", "N"),
    ("healthcheck", "::1", "N"),
    ("healthcheck", "localhost", "N"),
    ("mariadb.sys", "localhost", "N"),
    ("reader", "", "Y"),
    ("root", "%", "N"),
    ("root", "localhost", "N"),
]

ALICE_HASH = "*35E884F91E1624B27D0039433D98E922FA1F3075"
APP_HASH = "*3F57C84FDE4BBAB2C998F3A2D311684280BAE8E7"
BOB_HASH = "*6E97BE257172408566301E83709719270F834F3C"

MARIADB_SHOW = {
    "SHOW CREATE USER `alice`@`%`; SHOW GRANTS FOR `alice`@`%`": (
        f"CREATE USER `alice`@`%` IDENTIFIED BY PASSWORD '{ALICE_HASH}'\n"
        f"GRANT USAGE ON *.* TO `alice`@`%` IDENTIFIED BY PASSWORD '{ALICE_HASH}'\n"
        "GRANT SELECT, INSERT ON `appdb`.* TO `alice`@`%`\n"
        "GRANT UPDATE (`c`) ON `appdb`.`t` TO `alice`@`%`\n"
    ),
    "SHOW CREATE USER `app`@`%`; SHOW GRANTS FOR `app`@`%`": (
        f"CREATE USER `app`@`%` IDENTIFIED BY PASSWORD '{APP_HASH}'\n"
        f"GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '{APP_HASH}'\n"
        "GRANT ALL PRIVILEGES ON `appdb`.* TO `app`@`%`\n"
    ),
    "SHOW CREATE USER `bob`@`192.168.1.%`; SHOW GRANTS FOR `bob`@`192.168.1.%`": (
        f"CREATE USER `bob`@`192.168.1.%` IDENTIFIED BY PASSWORD '{BOB_HASH}' WITH MAX_USER_CONNECTIONS 5\n"
        "GRANT `reader` TO `bob`@`192.168.1.%`\n"
        f"GRANT USAGE ON *.* TO `bob`@`192.168.1.%` IDENTIFIED BY PASSWORD '{BOB_HASH}' WITH MAX_USER_CONNECTIONS 5\n"
        "GRANT SELECT ON `appdb`.`t` TO `bob`@`192.168.1.%` WITH GRANT OPTION\n"
        "SET DEFAULT ROLE `reader` FOR `bob`@`192.168.1.%`\n"
    ),
    "SHOW GRANTS FOR `reader`": "GRANT USAGE ON *.* TO `reader`\nGRANT SELECT ON `appdb`.* TO `reader`\n",
    "SHOW GRANTS FOR PUBLIC": "GRANT SELECT ON `appdb`.* TO PUBLIC\n",
}

ALICE_SHA2 = "0x2441243030352454547847092A7F162D46753434054035027C3F31516A776C37"
BOB_SHA2 = "0x244124303035242B6106052B573F454F4F757965060A6A2B7A353C78454A4641"
MYSQL_TAIL = (
    "PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK PASSWORD HISTORY DEFAULT PASSWORD REUSE INTERVAL DEFAULT "
    "PASSWORD REQUIRE CURRENT DEFAULT"
)
MYSQL_ACCOUNTS = [
    ("alice", "%", "N"),
    ("bob", "192.168.1.%", "N"),
    ("mysql.infoschema", "localhost", "N"),
    ("mysql.session", "localhost", "N"),
    ("mysql.sys", "localhost", "N"),
    ("reader", "%", "N"),
    ("root", "%", "N"),
    ("root", "localhost", "N"),
]
_HEX = "SET SESSION print_identified_with_as_hex = ON; "
MYSQL_SHOW = {
    f"{_HEX}SHOW CREATE USER `alice`@`%`; SHOW GRANTS FOR `alice`@`%`": (
        f"CREATE USER `alice`@`%` IDENTIFIED WITH 'caching_sha2_password' AS {ALICE_SHA2} REQUIRE NONE {MYSQL_TAIL}\n"
        "GRANT USAGE ON *.* TO `alice`@`%`\n"
        "GRANT SELECT, INSERT ON `appdb`.* TO `alice`@`%`\n"
    ),
    f"{_HEX}SHOW CREATE USER `bob`@`192.168.1.%`; SHOW GRANTS FOR `bob`@`192.168.1.%`": (
        f"CREATE USER `bob`@`192.168.1.%` IDENTIFIED WITH 'caching_sha2_password' AS {BOB_SHA2} "
        f"DEFAULT ROLE `reader`@`%` REQUIRE NONE WITH MAX_USER_CONNECTIONS 5 {MYSQL_TAIL}\n"
        "GRANT USAGE ON *.* TO `bob`@`192.168.1.%`\n"
        "GRANT SELECT ON `appdb`.`t` TO `bob`@`192.168.1.%` WITH GRANT OPTION\n"
        "GRANT `reader`@`%` TO `bob`@`192.168.1.%`\n"
    ),
    f"{_HEX}SHOW CREATE USER `reader`@`%`; SHOW GRANTS FOR `reader`@`%`": (
        "CREATE USER `reader`@`%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE PASSWORD EXPIRE "
        "ACCOUNT LOCK PASSWORD HISTORY DEFAULT PASSWORD REUSE INTERVAL DEFAULT PASSWORD REQUIRE CURRENT DEFAULT\n"
        "GRANT USAGE ON *.* TO `reader`@`%`\n"
        "GRANT SELECT ON `appdb`.* TO `reader`@`%`\n"
    ),
}


class FakeServer:
    """Answers the statements dump_accounts sends, like the real client."""

    def __init__(self, version: str, accounts: list[tuple[str, str, str]], show: dict[str, str]):
        self.version = version
        self.accounts = accounts
        self.show = dict(show)
        self.queries: list[str] = []

    def __call__(self, sql: str) -> str:
        self.queries.append(sql)
        if sql == accounts_module.VERSION_SQL:
            return f"{self.version}\n"
        if sql in (accounts_module.MARIADB_LIST_SQL, accounts_module.MYSQL_LIST_SQL):
            return _listing(self.accounts)
        if sql in self.show:
            return self.show[sql]
        raise AccountsDumpError(f"ERROR 1133 (28000) at line 1: unexpected statement {sql!r}")


def mariadb_server() -> FakeServer:
    return FakeServer(MARIADB_VERSION, MARIADB_ACCOUNTS, MARIADB_SHOW)


def mysql_server() -> FakeServer:
    return FakeServer(MYSQL_VERSION, MYSQL_ACCOUNTS, MYSQL_SHOW)


def _statements(sql: str) -> list[str]:
    return [line for line in sql.splitlines() if line and not line.startswith("--")]


# ---------------------------------------------------------------------------
# Building the dump
# ---------------------------------------------------------------------------


class TestDumpAccounts:
    def test_mariadb_dump_recreates_users_roles_and_grants(self):
        dump = dump_accounts(mariadb_server())

        assert dump.flavor == "mariadb"
        assert dump.sql.startswith(ACCOUNTS_HEADER + "\n")
        assert dump.sql.endswith("-- Dump completed (Arkive accounts dump: 3 users, 2 roles)\n")
        assert _statements(dump.sql) == [
            "CREATE ROLE IF NOT EXISTS `reader`;",
            f"CREATE USER IF NOT EXISTS `alice`@`%` IDENTIFIED BY PASSWORD '{ALICE_HASH}';",
            f"ALTER USER `alice`@`%` IDENTIFIED BY PASSWORD '{ALICE_HASH}';",
            f"CREATE USER IF NOT EXISTS `app`@`%` IDENTIFIED BY PASSWORD '{APP_HASH}';",
            f"ALTER USER `app`@`%` IDENTIFIED BY PASSWORD '{APP_HASH}';",
            f"CREATE USER IF NOT EXISTS `bob`@`192.168.1.%` IDENTIFIED BY PASSWORD '{BOB_HASH}' WITH MAX_USER_CONNECTIONS 5;",
            f"ALTER USER `bob`@`192.168.1.%` IDENTIFIED BY PASSWORD '{BOB_HASH}' WITH MAX_USER_CONNECTIONS 5;",
            "GRANT SELECT ON `appdb`.* TO PUBLIC;",
            f"GRANT USAGE ON *.* TO `alice`@`%` IDENTIFIED BY PASSWORD '{ALICE_HASH}';",
            "GRANT SELECT, INSERT ON `appdb`.* TO `alice`@`%`;",
            "GRANT UPDATE (`c`) ON `appdb`.`t` TO `alice`@`%`;",
            f"GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '{APP_HASH}';",
            "GRANT ALL PRIVILEGES ON `appdb`.* TO `app`@`%`;",
            "GRANT `reader` TO `bob`@`192.168.1.%`;",
            f"GRANT USAGE ON *.* TO `bob`@`192.168.1.%` IDENTIFIED BY PASSWORD '{BOB_HASH}' WITH MAX_USER_CONNECTIONS 5;",
            "GRANT SELECT ON `appdb`.`t` TO `bob`@`192.168.1.%` WITH GRANT OPTION;",
            "GRANT USAGE ON *.* TO `reader`;",
            "GRANT SELECT ON `appdb`.* TO `reader`;",
            # Default roles last: the role grants must exist first.
            "SET DEFAULT ROLE `reader` FOR `bob`@`192.168.1.%`;",
        ]
        assert {a.label for a, _ in dump.excluded} == {
            "healthcheck@127.0.0.1",
            "healthcheck@::1",
            "healthcheck@localhost",
            "mariadb.sys@localhost",
            "root@%",
            "root@localhost",
        }
        assert "root@% (root: its password comes from the new container's environment)" in dump.sql

    def test_mysql_dump_uses_hex_hashes_and_moves_default_roles(self):
        server = mysql_server()
        dump = dump_accounts(server)

        assert dump.flavor == "mysql"
        statements = _statements(dump.sql)
        assert statements[0] == (
            f"CREATE USER IF NOT EXISTS `alice`@`%` IDENTIFIED WITH 'caching_sha2_password' AS {ALICE_SHA2} "
            f"REQUIRE NONE {MYSQL_TAIL};"
        )
        assert statements[1] == statements[0].replace("CREATE USER IF NOT EXISTS ", "ALTER USER ", 1)
        bob = [s for s in statements if s.startswith(("CREATE USER IF NOT EXISTS `bob`", "ALTER USER `bob`"))]
        assert all("DEFAULT ROLE" not in s for s in bob[:2])
        assert "REQUIRE NONE WITH MAX_USER_CONNECTIONS 5" in bob[0]
        assert statements[-1] == "ALTER USER `bob`@`192.168.1.%` DEFAULT ROLE `reader`@`%`;"
        # MySQL roles are locked accounts: recreated like users.
        assert any(s.startswith("CREATE USER IF NOT EXISTS `reader`@`%`") for s in statements)
        assert "`root`" not in "\n".join(statements)
        assert not any("mysql.sys" in s or "mysql.session" in s for s in statements)
        assert all(q.startswith(_HEX) for q in server.queries if "SHOW CREATE USER" in q)

    def test_mysql_57_without_hex_hashes(self):
        server = FakeServer(
            "5.7.44",
            [("alice", "%", "N"), ("root", "localhost", "N"), ("mysql.sys", "localhost", "N")],
            {
                "SHOW CREATE USER `alice`@`%`; SHOW GRANTS FOR `alice`@`%`": (
                    f"CREATE USER 'alice'@'%' IDENTIFIED WITH 'mysql_native_password' AS '{ALICE_HASH}' "
                    "REQUIRE NONE PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK\n"
                    "GRANT USAGE ON *.* TO 'alice'@'%'\n"
                    "GRANT SELECT, INSERT ON `appdb`.* TO 'alice'@'%'\n"
                )
            },
        )
        dump = dump_accounts(server)
        assert _statements(dump.sql)[0] == (
            f"CREATE USER IF NOT EXISTS 'alice'@'%' IDENTIFIED WITH 'mysql_native_password' AS '{ALICE_HASH}' "
            "REQUIRE NONE PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK;"
        )
        assert not any("print_identified_with_as_hex" in q for q in server.queries)

    def test_binary_hash_without_hex_support_fails(self):
        # MySQL 5.7 / 8.0.16 print sha256_password hashes raw.
        server = FakeServer(
            "8.0.16",
            [("sha", "%", "N"), ("root", "localhost", "N")],
            {
                "SHOW CREATE USER `sha`@`%`; SHOW GRANTS FOR `sha`@`%`": (
                    "CREATE USER 'sha'@'%' IDENTIFIED WITH 'sha256_password' AS '$5$\x01\x1f;Ya\x06' REQUIRE NONE\n"
                    "GRANT USAGE ON *.* TO 'sha'@'%'\n"
                )
            },
        )
        with pytest.raises(AccountsDumpError, match="control characters"):
            dump_accounts(server)

    def test_hidden_password_fails(self):
        server = FakeServer(
            MYSQL_VERSION,
            [("alice", "%", "N"), ("root", "%", "N")],
            {
                f"{_HEX}SHOW CREATE USER `alice`@`%`; SHOW GRANTS FOR `alice`@`%`": (
                    "CREATE USER `alice`@`%` IDENTIFIED WITH 'caching_sha2_password' AS <secret> REQUIRE NONE\n"
                    "GRANT USAGE ON *.* TO `alice`@`%`\n"
                )
            },
        )
        with pytest.raises(AccountsDumpError, match="hid the password hash"):
            dump_accounts(server)

    def test_listing_without_root_is_incomplete(self):
        server = FakeServer(MARIADB_VERSION, [("alice", "%", "N")], MARIADB_SHOW)
        with pytest.raises(AccountsDumpError, match="does not include root"):
            dump_accounts(server)

    def test_client_error_propagates(self):
        server = mariadb_server()
        del server.show["SHOW CREATE USER `bob`@`192.168.1.%`; SHOW GRANTS FOR `bob`@`192.168.1.%`"]
        with pytest.raises(AccountsDumpError, match="1133"):
            dump_accounts(server)

    def test_unexpected_grant_output_fails(self):
        server = mariadb_server()
        server.show["SHOW GRANTS FOR `reader`"] = "GRANT USAGE ON *.* TO `reader`\nmysql: [Warning] something\n"
        with pytest.raises(AccountsDumpError, match="unexpected SHOW GRANTS output"):
            dump_accounts(server)

    def test_missing_create_statement_fails(self):
        server = mariadb_server()
        server.show["SHOW CREATE USER `alice`@`%`; SHOW GRANTS FOR `alice`@`%`"] = ""
        with pytest.raises(AccountsDumpError, match="no statement"):
            dump_accounts(server)

    def test_malformed_listing_fails(self):
        server = mariadb_server()

        def run(sql):
            return "not-hex\tzz\tN\n" if sql == accounts_module.MARIADB_LIST_SQL else server(sql)

        with pytest.raises(AccountsDumpError, match="unexpected account listing row"):
            dump_accounts(run)

    def test_names_are_quoted_as_identifiers(self):
        user = "we`ird; DROP USER x"
        server = FakeServer(
            MARIADB_VERSION,
            [(user, "%", "N"), ("root", "localhost", "N")],
            {
                "SHOW CREATE USER `we``ird; DROP USER x`@`%`; SHOW GRANTS FOR `we``ird; DROP USER x`@`%`": (
                    "CREATE USER `we``ird; DROP USER x`@`%` IDENTIFIED BY PASSWORD '*AB'\n"
                    "GRANT USAGE ON *.* TO `we``ird; DROP USER x`@`%` IDENTIFIED BY PASSWORD '*AB'\n"
                )
            },
        )
        dump = dump_accounts(server)
        assert "CREATE USER IF NOT EXISTS `we``ird; DROP USER x`@`%` IDENTIFIED BY PASSWORD '*AB';" in dump.sql

    @pytest.mark.parametrize(
        "account,excluded",
        [
            (Account("root", "%"), True),
            (Account("root", "localhost"), True),
            (Account("mysql.sys", "localhost"), True),
            (Account("mysql.session", "localhost"), True),
            (Account("mysql.infoschema", "localhost"), True),
            (Account("mariadb.sys", "localhost"), True),
            (Account("healthcheck", "localhost"), True),
            (Account("healthcheck", "::1"), True),
            (Account("debian-sys-maint", "localhost"), True),
            (Account("", "localhost"), True),
            # An application account that happens to be called healthcheck.
            (Account("healthcheck", "%"), False),
            (Account("rooted", "%"), False),
            (Account("app", "%"), False),
            (Account("PUBLIC", "", is_role=True), False),
        ],
    )
    def test_excluded_accounts(self, account, excluded):
        assert (excluded_reason(account) is not None) is excluded


# ---------------------------------------------------------------------------
# DBDumper integration
# ---------------------------------------------------------------------------

MYSQL_DUMP = (
    b"-- MariaDB dump 10.19  Distrib 11.4.13-MariaDB, for debian-linux-gnu (x86_64)\n--\n"
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `appdb`;\nUSE `appdb`;\n"
    + b"INSERT INTO `t` VALUES (1),(2),(3);\n" * 10
    + b"\n-- Dump completed on 2026-10-22 22:00:00\n"
)


def _server_container(name, env, server: FakeServer, tags=("mariadb:11.4",), client_ok=True):
    container = MagicMock()
    container.name = name
    container.status = "running"
    container.image.tags = list(tags)
    container.attrs = {"Config": {"Env": env, "Image": tags[0]}, "Mounts": []}
    container.client.api = None
    container.calls = []

    def exec_run(cmd, demux=False, stream=False, environment=None, **kwargs):
        cmd = list(cmd)
        container.calls.append({"cmd": cmd, "environment": environment})
        if cmd[0] in ("mariadb-dump", "mysqldump"):
            items = [(MYSQL_DUMP, None)]
            return (0, iter(items)) if stream else (0, (MYSQL_DUMP, None))
        assert cmd[0] in ("mariadb", "mysql")
        sql = cmd[-1].removeprefix("--execute=")
        if not client_ok:
            return 1, (None, b"ERROR 2002 (HY000): Can't connect to local server through socket\n")
        try:
            return 0, (server(sql).encode() or None, None)
        except AccountsDumpError as e:
            return 1, (None, f"{e}\n".encode())

    container.exec_run.side_effect = exec_run
    return container


def _docker(*containers):
    client = MagicMock()
    by_name = {c.name: c for c in containers}
    client.containers.get.side_effect = lambda name: by_name[name]
    return client


def _maria_db(container="mdb", name="appdb"):
    return DiscoveredDatabase(container_name=container, db_type="mariadb", db_name=name)


def _accounts(results):
    return [r for r in results if r.kind == ACCOUNTS_KIND]


class TestDumperIntegration:
    @pytest.mark.asyncio
    async def test_one_accounts_dump_per_server(self, mock_config):
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], mariadb_server())
        results = await DBDumper(_docker(maria), mock_config).dump_all(
            [_maria_db("mdb", "appdb"), _maria_db("mdb", "photos")]
        )

        accounts = _accounts(results)
        assert len(accounts) == 1
        result = accounts[0]
        assert (result.status, result.db_type, result.db_name, result.integrity_check) == (
            "success",
            "mariadb",
            ACCOUNTS_DB_NAME,
            "ok",
        )
        assert result.db_key == dump_prefix("mdb", "mariadb", ACCOUNTS_DB_NAME, kind=ACCOUNTS_KIND)
        assert os.path.basename(result.dump_path).startswith(result.db_key + "_")
        text = Path(result.dump_path).read_text()
        assert text == dump_accounts(mariadb_server()).sql
        assert result.dump_size_bytes == len(text.encode())

        client_calls = [c for c in maria.calls if c["cmd"][0] == "mariadb"]
        assert client_calls, "the server's own client is used"
        for call in client_calls:
            assert call["cmd"][:7] == ["mariadb", "-u", "root", "--connect-timeout=10", "-N", "-B", "-r"]
            assert call["cmd"][7].startswith("--execute=")
            assert len(call["cmd"]) == 8
            assert call["environment"] == {"MYSQL_PWD": "pw"}
        assert not [n for n in os.listdir(mock_config.dump_dir) if n.endswith(".partial")]

    @pytest.mark.asyncio
    async def test_mysql_image_uses_mysql_client(self, mock_config):
        mysql = _server_container("my", ["MYSQL_ROOT_PASSWORD=pw"], mysql_server(), tags=("mysql:8.4",))
        results = await DBDumper(_docker(mysql), mock_config).dump_all([_maria_db("my")])
        assert _accounts(results)[0].status == "success"
        assert {c["cmd"][0] for c in mysql.calls} == {"mysqldump", "mysql"}

    @pytest.mark.asyncio
    async def test_without_root_password_is_skipped_with_reason(self, mock_config):
        maria = _server_container(
            "mdb", ["MARIADB_USER=app", "MARIADB_PASSWORD=app-pass", "MARIADB_RANDOM_ROOT_PASSWORD=1"], mariadb_server()
        )
        results = await DBDumper(_docker(maria), mock_config).dump_all([_maria_db()])
        result = _accounts(results)[0]
        assert result.status == "skipped"
        assert "MARIADB_ROOT_PASSWORD" in result.error and "(app)" in result.error
        assert not [c for c in maria.calls if c["cmd"][0] == "mariadb"]
        assert not [n for n in os.listdir(mock_config.dump_dir) if "_accounts-" in n]

    @pytest.mark.asyncio
    async def test_failure_keeps_the_previous_good_dump(self, mock_config):
        good_server = mariadb_server()
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], good_server)
        first = _accounts(await DBDumper(_docker(maria), mock_config).dump_all([_maria_db()]))[0]
        assert first.status == "success"
        time.sleep(1.1)  # the next dump gets a newer timestamp

        broken = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], mariadb_server(), client_ok=False)
        second = _accounts(await DBDumper(_docker(broken), mock_config).dump_all([_maria_db()]))[0]
        assert second.status == "failed"
        assert "users and grants could not be dumped" in second.error and "ERROR 2002" in second.error
        files = [n for n in os.listdir(mock_config.dump_dir) if "_accounts-" in n]
        assert files == [os.path.basename(first.dump_path)]
        manifest = read_dump_manifest(mock_config.dump_dir)
        assert [e["file"] for e in manifest["dumps"] if e["kind"] == ACCOUNTS_KIND] == files

    @pytest.mark.asyncio
    async def test_incomplete_server_answer_fails_without_a_file(self, mock_config):
        server = mariadb_server()
        server.show["SHOW GRANTS FOR `reader`"] = ""
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], server)
        result = _accounts(await DBDumper(_docker(maria), mock_config).dump_all([_maria_db()]))[0]
        assert result.status == "failed" and "SHOW GRANTS returned nothing" in result.error
        assert not [n for n in os.listdir(mock_config.dump_dir) if "_accounts-" in n or n.endswith(".partial")]

    @pytest.mark.asyncio
    async def test_gzip_setting_applies(self, mock_config):
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], mariadb_server())
        dumper = DBDumper(_docker(maria), mock_config)
        dumper._load_compression = AsyncMock(return_value="gzip")
        result = _accounts(await dumper.dump_all([_maria_db()]))[0]
        assert result.status == "success" and result.dump_path.endswith(".sql.gz")
        with gzip.open(result.dump_path, "rt") as handle:
            assert handle.read().startswith(ACCOUNTS_HEADER)

    @pytest.mark.asyncio
    async def test_manifest_entry_and_inference(self, mock_config):
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], mariadb_server())
        result = _accounts(await DBDumper(_docker(maria), mock_config).dump_all([_maria_db()]))[0]
        entries = json.loads((mock_config.dump_dir / DUMP_MANIFEST_NAME).read_text())["dumps"]
        entry = next(e for e in entries if e["kind"] == ACCOUNTS_KIND)
        assert entry["file"] == os.path.basename(result.dump_path)
        assert (entry["container_name"], entry["db_type"], entry["db_name"], entry["integrity"]) == (
            "mdb",
            "mariadb",
            ACCOUNTS_DB_NAME,
            "ok",
        )
        # Without recorded metadata (manifest lost), the file is recognised from its header.
        (mock_config.dump_dir / DUMP_MANIFEST_NAME).unlink()
        inferred = write_dump_manifest(mock_config.dump_dir, known_containers={"mdb"})["dumps"]
        entry = next(e for e in inferred if e["file"] == os.path.basename(result.dump_path))
        assert (entry["container_name"], entry["db_type"], entry["db_name"], entry["kind"]) == (
            "mdb",
            "mariadb",
            ACCOUNTS_DB_NAME,
            ACCOUNTS_KIND,
        )

    @pytest.mark.asyncio
    async def test_verify_engine_sees_a_complete_dump(self, mock_config, tmp_path):
        maria = _server_container("mdb", ["MARIADB_ROOT_PASSWORD=pw"], mariadb_server())
        result = _accounts(await DBDumper(_docker(maria), mock_config).dump_all([_maria_db()]))[0]
        assert VerifyEngine._check_plain_sql_dump(result.dump_path)["status"] == "success"
        truncated = tmp_path / os.path.basename(result.dump_path)
        truncated.write_text(Path(result.dump_path).read_text().rsplit("-- Dump completed", 1)[0])
        assert VerifyEngine._check_plain_sql_dump(str(truncated))["status"] == "failed"

    @pytest.mark.asyncio
    async def test_no_accounts_dump_without_mysql(self, mock_config):
        results = await DBDumper(_docker(), mock_config).dump_all([])
        assert _accounts(results) == []

    def test_database_named_accounts_does_not_collide(self):
        assert dump_prefix("mdb", "mariadb", "accounts") != dump_prefix(
            "mdb", "mariadb", "accounts", kind=ACCOUNTS_KIND
        )
        assert dump_prefix("mdb", "mariadb", "accounts", kind=ACCOUNTS_KIND) in server_dump_keys("mdb")


# ---------------------------------------------------------------------------
# Cleanup, run status and size checks
# ---------------------------------------------------------------------------


class TestCleanupAndHealth:
    def test_old_accounts_dump_of_a_present_server_is_kept(self, tmp_path):
        key = dump_prefix("mdb", "mariadb", ACCOUNTS_DB_NAME, kind=ACCOUNTS_KIND)
        old = tmp_path / f"{key}_20200101_000000.sql"
        old.write_text("x")
        ancient = time.time() - 30 * 86400
        os.utime(old, (ancient, ancient))

        cleanup_dump_dir(tmp_path, max_age_days=7, protect_prefixes={key})
        assert old.exists()
        cleanup_dump_dir(tmp_path, max_age_days=7, protect_prefixes=set())
        assert not old.exists()  # the server is gone: aged out like its databases

    def test_orchestrator_protects_accounts_dumps(self, tmp_path):
        from app.services.orchestrator import BackupOrchestrator

        orch = BackupOrchestrator.__new__(BackupOrchestrator)
        orch.dump_max_age_days = 7
        dbs = [_maria_db("mdb", "appdb"), DiscoveredDatabase(container_name="pg", db_type="postgres", db_name="app")]
        container = MagicMock(status="running", databases=dbs, warnings=[])
        _age, protect, _legacy = orch._dump_cleanup_policy(True, [container], dbs)
        assert dump_prefix("mdb", "mariadb", ACCOUNTS_DB_NAME, kind=ACCOUNTS_KIND) in protect
        assert dump_prefix("pg", "mariadb", ACCOUNTS_DB_NAME, kind=ACCOUNTS_KIND) not in protect

    @pytest.mark.asyncio
    async def test_size_check_ignores_accounts_dumps(self, tmp_path):
        import aiosqlite

        from app.core.database import init_db

        db_path = tmp_path / "arkive.db"
        await init_db(db_path)
        accounts = DumpResult(
            container_name="mdb",
            db_type="mariadb",
            db_name=ACCOUNTS_DB_NAME,
            dump_path=str(tmp_path / "x.sql"),
            dump_size_bytes=10,
            integrity_check="ok",
            status="success",
            kind=ACCOUNTS_KIND,
        )
        async with aiosqlite.connect(db_path) as db:
            outcome = await check_dump_sizes(db, "run-1", [accounts])
        assert outcome.warnings == [] and outcome.flagged == set()


# ---------------------------------------------------------------------------
# Restore plan, drill, guided restore
# ---------------------------------------------------------------------------


class TestRestorePaths:
    def test_plan_restores_accounts_after_the_servers_databases(self, mock_config):
        gen = RestorePlanGenerator(mock_config)
        rows = [
            {"container_name": "mdb", "db_type": "mariadb", "db_name": "appdb"},
            {"container_name": "pg", "db_type": "postgres", "db_name": "app"},
            {"container_name": "mdb", "db_type": "mariadb", "db_name": "photos"},
            {"container_name": "my", "db_type": "mysql", "db_name": "shop"},
        ]
        ordered = gen._with_accounts_steps([{**r, "restore_cmd": "", "full_restore_cmd": ""} for r in rows])
        assert [(r["container_name"], r["db_name"]) for r in ordered] == [
            ("mdb", "appdb"),
            ("pg", "app"),
            ("mdb", "photos"),
            ("mdb", ACCOUNTS_STEP_LABEL),
            ("my", "shop"),
            ("my", ACCOUNTS_STEP_LABEL),
        ]
        full = ordered[3]["full_restore_cmd"]
        glob = f"{dump_prefix('mdb', 'mariadb', ACCOUNTS_DB_NAME, kind=ACCOUNTS_KIND)}_[0-9]*.sql"
        assert f"cat {glob} | docker exec -i mdb sh -c" in full
        assert f"gunzip -c {glob}.gz |" in full
        assert '${p:+"-p$p"}' in full  # never a bare -p

    def test_drill_skips_accounts_dump_with_reason(self):
        manifest = {
            "dumps": [
                {"file": "mdb_appdb-0000aaaa_20261022_220000.sql", "container_name": "mdb", "db_type": "mariadb",
                 "db_name": "appdb", "kind": "database"},
                {"file": "mdb_accounts-0000bbbb_20261022_220000.sql", "container_name": "mdb", "db_type": "mariadb",
                 "db_name": "accounts", "kind": "accounts"},
            ]
        }  # fmt: skip
        files = {e["file"]: {"size": 100} for e in manifest["dumps"]}
        candidates = candidates_from_manifest(manifest, files)
        chosen, excluded = select_candidates(candidates, {}, 10, 10**9)
        assert [c.db_name for c in chosen] == ["appdb"]
        assert [(c.kind, status) for c, status, _ in excluded] == [(ACCOUNTS_KIND, "skipped")]
        assert "not loaded by drills" in excluded[0][2]

    def test_drill_inference_tells_accounts_dump_from_database_named_accounts(self):
        accounts_file = f"{dump_prefix('mdb', 'mariadb', 'accounts', kind=ACCOUNTS_KIND)}_20261022_220000.sql"
        database_file = f"{dump_prefix('mdb', 'mariadb', 'accounts')}_20261022_220000.sql"
        files = {accounts_file: {"size": 10}, database_file: {"size": 10}}
        known = [
            {"container_name": "mdb", "db_type": "mariadb", "db_name": "accounts"},
        ]
        by_file = {c.file: c for c in infer_candidates(files, known)}
        assert (by_file[accounts_file].kind, by_file[accounts_file].db_name) == (ACCOUNTS_KIND, "accounts")
        assert (by_file[database_file].kind, by_file[database_file].db_name) == ("database", "accounts")

    def test_guided_restore_never_picks_the_accounts_dump(self, mock_config):
        from app.services.guided_restore import DatabaseRef, GuidedRestoreEngine

        service = GuidedRestoreEngine.__new__(GuidedRestoreEngine)
        accounts_file = f"{dump_prefix('mdb', 'mariadb', 'accounts', kind=ACCOUNTS_KIND)}_20261023_220000.sql"
        database_file = f"{dump_prefix('mdb', 'mariadb', 'accounts')}_20261022_220000.sql"
        files = {accounts_file: {"size": 10}, database_file: {"size": 10}}
        ref = DatabaseRef(container_name="mdb", db_type="mariadb", db_name="accounts", host_path=None)
        assert service._match_file_names(files, ref).file == database_file
        manifest = {
            "dumps": [
                {"file": accounts_file, "container_name": "mdb", "db_type": "mariadb", "db_name": "accounts",
                 "kind": "accounts", "created_at": "2026-10-23T22:00:00Z"},
                {"file": database_file, "container_name": "mdb", "db_type": "mariadb", "db_name": "accounts",
                 "kind": "database", "created_at": "2026-10-22T22:00:00Z"},
            ]
        }  # fmt: skip
        assert service._match_manifest(manifest, files, ref).file == database_file
