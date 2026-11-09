"""Fake database containers for guided-restore tests.

They behave like the real thing where Arkive can observe it:

- short commands go through ``exec_run`` (non-streaming: real exit code);
- dumps (pg_dump / mysqldump) stream realistic output with completion trailers;
- loads arrive over the low-level exec API with stdin attached
  (``exec_create`` / ``exec_start(socket=True)`` / ``exec_inspect``) on a real
  socket pair, framed like Docker's multiplexed stream;
- the "database" keeps tables and rows, so a test can tell which data a
  restore (or an undo) left behind.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

PG_TRAILER = "--\n-- PostgreSQL database dump complete\n--\n\n"
MYSQL_TRAILER = "-- Dump completed on 2026-11-01 10:00:00\n"


def pg_dump_text(db_name: str, rows: list[str], tables: tuple[str, ...] = ("users", "notes")) -> bytes:
    """pg_dump --clean --create --if-exists --no-owner output for ``db_name``."""
    lines = [
        "--",
        "-- PostgreSQL database dump",
        "--",
        "SET statement_timeout = 0;",
        "SET client_encoding = 'UTF8';",
        f"DROP DATABASE IF EXISTS {db_name};",
        f"CREATE DATABASE {db_name} WITH TEMPLATE = template0 ENCODING = 'UTF8';",
        f"\\connect {db_name}",
        "SELECT pg_catalog.set_config('search_path', '', false);",
    ]
    for table in tables:
        lines.append(f"CREATE TABLE public.{table} (\n    id integer NOT NULL,\n    name text\n);")
    if tables:
        lines.append(f"COPY public.{tables[0]} (id, name) FROM stdin;")
        lines += [f"{i}\t{name}" for i, name in enumerate(rows, start=1)]
        lines.append("\\.")
    return ("\n".join(lines) + "\n" + PG_TRAILER).encode()


def mysql_dump_text(db_name: str, rows: list[str], tables: tuple[str, ...] = ("users", "notes")) -> bytes:
    """mysqldump --databases output for ``db_name``."""
    lines = [
        "-- MariaDB dump 10.19  Distrib 10.11.6-MariaDB, for debian-linux-gnu (x86_64)",
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;",
        f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{db_name}` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;",
        f"USE `{db_name}`;",
    ]
    for table in tables:
        lines += [
            f"DROP TABLE IF EXISTS `{table}`;",
            f"CREATE TABLE `{table}` (\n  `id` int(11) NOT NULL,\n  `name` varchar(100)\n) ENGINE=InnoDB;",
        ]
    values = ",".join(f"({i},'{name}')" for i, name in enumerate(rows, start=1))
    if values:
        lines.append(f"INSERT INTO `{tables[0]}` VALUES {values};")
    return ("\n".join(lines) + "\n" + MYSQL_TRAILER).encode()


class FakeExecAPI:
    """docker-py's APIClient exec calls, with stdin delivered over a socket pair."""

    def __init__(self, container: FakeDbContainer):
        self.container = container
        self.execs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def exec_create(self, container_id, cmd, stdout=True, stderr=True, stdin=False, environment=None, **kwargs):
        with self._lock:
            exec_id = f"exec-{len(self.execs) + 1}"
            self.execs[exec_id] = {
                "cmd": list(cmd),
                "env": dict(environment or {}),
                "stdin": stdin,
                "running": False,
                "exit_code": None,
            }
        return {"Id": exec_id}

    def exec_start(self, exec_id, socket=False, **kwargs):  # noqa: A002 - docker-py's name
        assert socket, "loads must use a raw socket"
        record = self.execs[exec_id]
        ours, theirs = _socketpair()
        record["running"] = True

        def serve() -> None:
            data = bytearray()
            limit = self.container.read_limit
            while True:
                chunk = theirs.recv(65536)
                if not chunk:
                    break
                data += chunk
                if limit is not None and len(data) >= limit:
                    break  # the process "exited" without reading everything
            code, out, err = self.container.run(record["cmd"], record["env"], bytes(data))
            frames = b""
            for stream_id, payload in ((1, out), (2, err)):
                if payload:
                    frames += struct.pack(">BxxxL", stream_id, len(payload)) + payload
            record["exit_code"] = code
            record["running"] = False
            try:
                theirs.sendall(frames)
            except OSError:
                pass
            theirs.close()

        threading.Thread(target=serve, daemon=True).start()
        return ours

    def exec_inspect(self, exec_id):
        record = self.execs[exec_id]
        return {"Running": record["running"], "ExitCode": record["exit_code"]}


def _socketpair():
    return socket.socketpair()


class FakeDbContainer:
    """Common container surface (attrs, status, stop/start, exec_run)."""

    def __init__(self, name: str, env: dict[str, str], image: str, status: str = "running"):
        self.name = name
        self.id = f"id-{name}"
        self.status = status
        self.labels: dict[str, str] = {}
        self.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()], "Image": image}}
        self.image = SimpleNamespace(tags=[image])
        self.client = SimpleNamespace(api=FakeExecAPI(self))
        self.calls: list[dict] = []
        # Stop reading stdin after this many bytes (simulates an early exit).
        self.read_limit: int | None = None

    def exec_run(self, cmd, environment=None, demux=False, stream=False, **kwargs):
        code, out, err = self.run(list(cmd), dict(environment or {}), b"")
        if stream:
            chunks = [(out[i : i + 512], None) for i in range(0, len(out), 512)]
            if err:
                chunks.append((None, err))
            return (code, iter(chunks) if demux else iter([out]))
        return (code, (out or None, err or None) if demux else out + err)

    def reload(self):
        pass

    def stop(self, timeout=None):
        self.calls.append({"cmd": ["<stop>"], "env": {}, "stdin": b""})
        self.status = "exited"

    def start(self):
        self.calls.append({"cmd": ["<start>"], "env": {}, "stdin": b""})
        self.status = "running"

    def run(self, cmd: list[str], env: dict[str, str], stdin: bytes) -> tuple[int, bytes, bytes]:
        raise NotImplementedError

    def commands(self, program: str) -> list[dict]:
        return [c for c in self.calls if c["cmd"] and c["cmd"][0] == program]


class FakeAppContainer(FakeDbContainer):
    def __init__(self, name: str, status: str = "running"):
        super().__init__(name, {}, "ghcr.io/example/app:latest", status)

    def run(self, cmd, env, stdin):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        return 127, b"", b"not a database\n"


class FakePostgres(FakeDbContainer):
    """A Postgres cluster: databases with an oid, an owner and rows in their first table."""

    def __init__(self, name: str = "pg", user: str = "postgres", databases: dict | None = None, superuser=True):
        super().__init__(name, {"POSTGRES_USER": user, "POSTGRES_PASSWORD": "pg-secret"}, "postgres:16")
        self.user = user
        self.superuser = superuser
        self.databases: dict[str, dict] = {}
        self._next_oid = 16384
        for db_name, spec in (databases or {}).items():
            self.add_database(db_name, **spec)
        self.sessions = 2
        # Failure switches.
        self.fail_drop = False  # DROP DATABASE refused (sessions still connected)
        self.fail_after_drop = False  # error after the database was re-created
        self.fail_owner_fix = False
        self.dump_truncated = False
        self.owner_fix_sql: list[str] = []

    def add_database(self, db_name: str, rows=(), owner: str | None = None, tables=("users", "notes")):
        self._next_oid += 1
        self.databases[db_name] = {
            "oid": self._next_oid,
            "owner": owner or self.user,
            "rows": list(rows),
            "tables": list(tables),
            "allowconn": True,
        }

    def _state(self, db_name: str) -> dict:
        db = self.databases.get(db_name)
        return {
            "user": self.user,
            "superuser": self.superuser,
            "createdb": True,
            "version": 160004,
            "exists": db is not None,
            "oid": str(db["oid"]) if db else None,
            "allowconn": db["allowconn"] if db else None,
            "owner": db["owner"] if db else None,
            "sessions": self.sessions if db else 0,
        }

    def run(self, cmd, env, stdin):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        program = cmd[0]
        if program == "pg_dump":
            db_name = cmd[cmd.index("-d") + 1]
            db = self.databases.get(db_name)
            if db is None:
                return 1, b"", f'pg_dump: error: database "{db_name}" does not exist\n'.encode()
            text = pg_dump_text(db_name, db["rows"], tuple(db["tables"]))
            if self.dump_truncated:
                text = text[: len(text) // 2]
            return 0, text, b""
        if program != "psql":
            return 127, b"", f'exec: "{program}": executable file not found in $PATH\n'.encode()
        database = cmd[cmd.index("-d") + 1]
        if database not in self.databases and database not in ("postgres", "template1"):
            return 2, b"", f'psql: error: FATAL:  database "{database}" does not exist\n'.encode()
        if "-c" in cmd:
            return self._command(database, cmd[cmd.index("-c") + 1])
        return self._load(stdin)

    def _command(self, database: str, sql: str):
        if sql == "SELECT 1":
            return 0, b"1\n", b""
        if sql.startswith("SELECT json_build_object("):
            name = re.search(r"datname = '((?:[^']|'')*)'", sql).group(1).replace("''", "'")
            return 0, (json.dumps(self._state(name)) + "\n").encode(), b""
        match = re.match(r'ALTER DATABASE "((?:[^"]|"")*)" WITH ALLOW_CONNECTIONS true$', sql)
        if match:
            self.databases[match.group(1).replace('""', '"')]["allowconn"] = True
            return 0, b"", b""
        match = re.match(r'ALTER DATABASE "((?:[^"]|"")*)" OWNER TO "((?:[^"]|"")*)";', sql)
        if match:
            self.owner_fix_sql.append(sql)
            if self.fail_owner_fix:
                return 1, b"", b'ERROR:  role "app" does not exist\n'
            self.databases[match.group(1)]["owner"] = match.group(2)
            return 0, b"DO\n", b""
        if "FROM pg_class c JOIN pg_namespace" in sql:
            return 0, f"{len(self.databases[database]['tables'])}\n".encode(), b""
        return 1, b"", f"ERROR:  unexpected statement in fake: {sql[:80]}\n".encode()

    def _load(self, script: bytes):
        text = script.decode()
        lines = text.splitlines()
        current = None
        for number, line in enumerate(lines, start=1):
            match = re.match(r'ALTER DATABASE "((?:[^"]|"")*)" WITH ALLOW_CONNECTIONS false;', line)
            if match:
                self.databases[match.group(1)]["allowconn"] = False
                continue
            match = re.match(r"DROP DATABASE IF EXISTS (.+);", line)
            if match:
                name = match.group(1).strip('"')
                if self.fail_drop and name in self.databases:
                    return (
                        3,
                        b"",
                        (
                            f'psql:<stdin>:{number}: ERROR:  database "{name}" is being accessed by other users\n'
                        ).encode(),
                    )
                self.databases.pop(name, None)
                continue
            match = re.match(r"CREATE DATABASE (\S+) ", line)
            if match:
                current = match.group(1).strip('"')
                self._next_oid += 1
                self.databases[current] = {
                    "oid": self._next_oid,
                    "owner": self.user,
                    "rows": [],
                    "tables": [],
                    "allowconn": True,
                }
                if self.fail_after_drop:
                    return 3, b"", f'psql:<stdin>:{number + 2}: ERROR:  extension "vectors" is not available\n'.encode()
                continue
            match = re.match(r"CREATE TABLE public\.(\w+)", line)
            if match and current:
                self.databases[current]["tables"].append(match.group(1))
                continue
            match = re.match(r"^\d+\t(.*)$", line)
            if match and current:
                self.databases[current]["rows"].append(match.group(1))
        if "PostgreSQL database dump complete" not in text:
            return 3, b"", b"psql:<stdin>: ERROR:  unexpected end of input\n"
        return 0, b" set_config \n------------\n \n(1 row)\n\n", b""


class FakeMariaDB(FakeDbContainer):
    def __init__(self, name: str = "mariadb", databases: dict | None = None, client: str = "mariadb"):
        super().__init__(name, {"MYSQL_ROOT_PASSWORD": "root-secret", "MYSQL_DATABASE": "appdb"}, "mariadb:11")
        self.client_binary = client
        self.databases: dict[str, dict] = {
            db_name: {"rows": list(rows), "tables": ["users", "notes"]} for db_name, rows in (databases or {}).items()
        }
        self.fail_load = False

    def run(self, cmd, env, stdin):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        program = cmd[0]
        if program in ("mariadb-dump", "mysqldump"):
            db_name = cmd[cmd.index("--databases") + 1]
            db = self.databases.get(db_name)
            if db is None:
                return 2, b"", f"mysqldump: Got error: 1049: Unknown database '{db_name}'\n".encode()
            return 0, mysql_dump_text(db_name, db["rows"], tuple(db["tables"])), b""
        if program != self.client_binary:
            return 127, b"", f'exec: "{program}": executable file not found in $PATH\n'.encode()
        if cmd[1:] == ["--version"]:
            return 0, f"{program}  Ver 15.1 Distrib 11.4.2-MariaDB\n".encode(), b""
        if "-e" in cmd:
            sql = cmd[cmd.index("-e") + 1]
            name = re.search(r"= '((?:[^'\\]|''|\\\\)*)'", sql).group(1)
            if "information_schema.SCHEMATA" in sql:
                return 0, f"{1 if name in self.databases else 0}\n".encode(), b""
            if "information_schema.TABLES" in sql:
                return 0, f"{len(self.databases.get(name, {}).get('tables', []))}\n".encode(), b""
            return 1, b"", b"ERROR 1064 (42000): unexpected statement in fake\n"
        return self._load(stdin)

    def _load(self, script: bytes):
        text = script.decode()
        current = None
        for line in text.splitlines():
            match = re.match(r"USE `((?:[^`]|``)+)`;", line)
            if match:
                current = match.group(1)
                self.databases.setdefault(current, {"rows": [], "tables": []})
                continue
            if line.startswith("PREPARE arkive_stmt") and current:
                # The prelude's prepared DROP TABLE / DROP VIEW statements.
                self.databases[current] = {"rows": [], "tables": []}
                continue
            if self.fail_load and line.startswith("CREATE TABLE"):
                return 1, b"", b"ERROR 1005 (HY000) at line 9: Can't create table `appdb`.`users`\n"
            match = re.match(r"CREATE TABLE `(\w+)`", line)
            if match and current:
                self.databases[current]["tables"].append(match.group(1))
                continue
            if line.startswith("INSERT INTO") and current:
                self.databases[current]["rows"] += re.findall(r"\(\d+,'([^']*)'\)", line)
        return 0, b"", b""


class FakeDockerForRestore:
    """``docker_client.containers.get`` over a dict of fake containers."""

    def __init__(self, *containers: FakeDbContainer):
        self.by_name = {c.name: c for c in containers}
        self.containers = SimpleNamespace(get=self._get)

    def _get(self, name):
        if name not in self.by_name:
            raise LookupError(f"No such container: {name}")
        return self.by_name[name]


class FakeSnapshotBackupEngine:
    """BackupEngine.ls / restore for one snapshot whose dump directory holds ``files``."""

    def __init__(self, dump_dir, files: dict[str, bytes], full_id: str):
        self.dump_dir = str(dump_dir).rstrip("/")
        self.files = files
        self.full_id = full_id
        self.restores: list[dict] = []
        self.ls_error: Exception | None = None

    async def ls(self, target, snapshot_id, path="/", strict=False):
        assert snapshot_id == self.full_id
        if self.ls_error:
            raise self.ls_error
        assert path == self.dump_dir
        return [{"name": name, "type": "file", "size": len(data)} for name, data in self.files.items()]

    async def restore(self, target, snapshot_id, paths=None, restore_to=None):
        assert snapshot_id == self.full_id
        self.restores.append({"paths": list(paths or []), "restore_to": restore_to})
        for path in paths or []:
            name = os.path.basename(path)
            if name not in self.files:
                return {"status": "failed", "error": f"no such file {path}"}
            dest = Path(restore_to) / path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.files[name])
        return {"status": "success", "output": "", "error": None}
