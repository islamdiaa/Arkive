"""Fake MongoDB and Redis containers for guided-restore tests.

Like fake_db_server.py, they behave the way Arkive can observe the real thing
(checked against mongo:7.0/8.0 and redis:6.2/7.4/8.0):

- mongodump writes a real ``--archive`` stream (magic number, prelude, BSON
  documents, per-collection EOF headers with the CRC-64/ECMA mongodump
  writes); mongorestore reads one from stdin and fails on a truncated one;
- mongosh runs the scripts guided restore sends on stdin (recognised by what
  they do), logs in with the credentials in the script, and fails with exit
  code 1 the way ``mongosh --file`` does;
- redis-cli answers CONFIG GET / INFO like Redis does without a TTY
  (an error reply is printed with exit code 0).
"""

from __future__ import annotations

import json
import re
import struct
from types import SimpleNamespace

import docker

from app.services.dump_checks import MONGO_ARCHIVE_MAGIC, crc64_ecma, crc64_jones
from tests.fakes.fake_db_server import FakeDbContainer, FakeExecAPI

# ---------------------------------------------------------------------------
# BSON / archive encoding
# ---------------------------------------------------------------------------


def _cstring(text: str) -> bytes:
    return text.encode() + b"\x00"


def bson(fields: dict, int64_keys: tuple[str, ...] = ()) -> bytes:
    body = b""
    for key, value in fields.items():
        if isinstance(value, bool):
            body += b"\x08" + _cstring(key) + (b"\x01" if value else b"\x00")
        elif isinstance(value, int) and (key in int64_keys or not -(2**31) <= value < 2**31):
            body += b"\x12" + _cstring(key) + struct.pack("<q", value)
        elif isinstance(value, int):
            body += b"\x10" + _cstring(key) + struct.pack("<i", value)
        elif isinstance(value, str):
            raw = value.encode()
            body += b"\x02" + _cstring(key) + struct.pack("<i", len(raw) + 1) + raw + b"\x00"
        elif isinstance(value, dict):
            body += b"\x03" + _cstring(key) + bson(value)
        else:
            raise TypeError(type(value))
    return struct.pack("<i", len(body) + 5) + body + b"\x00"


def unbson(doc: bytes) -> dict:
    out: dict = {}
    pos = 4
    while doc[pos] != 0:
        kind = doc[pos]
        end = doc.index(b"\x00", pos + 1)
        key = doc[pos + 1 : end].decode()
        pos = end + 1
        if kind == 0x08:
            out[key] = doc[pos] == 1
            pos += 1
        elif kind == 0x10:
            (out[key],) = struct.unpack_from("<i", doc, pos)
            pos += 4
        elif kind == 0x12:
            (out[key],) = struct.unpack_from("<q", doc, pos)
            pos += 8
        elif kind == 0x02:
            (length,) = struct.unpack_from("<i", doc, pos)
            out[key] = doc[pos + 4 : pos + 3 + length].decode()
            pos += 4 + length
        elif kind == 0x03:
            (length,) = struct.unpack_from("<i", doc, pos)
            out[key] = unbson(doc[pos : pos + length])
            pos += length
        else:
            raise ValueError(f"unsupported BSON type {kind}")
    return out


TERMINATOR = struct.pack("<i", -1)


def mongo_archive(databases: dict[str, dict[str, list[dict]]], views: dict[str, list[str]] | None = None) -> bytes:
    """A ``mongodump --archive`` of ``{db: {collection: [documents]}}``, as mongodump 100.x writes it."""
    data = struct.pack("<I", MONGO_ARCHIVE_MAGIC)
    data += bson({"concurrent_collections": 4, "version": "0.1", "server_version": "8.0.4", "tool_version": "100.10.0"})
    namespaces = [(db, coll, "collection") for db, colls in databases.items() for coll in colls]
    namespaces += [(db, view, "view") for db, names in (views or {}).items() for view in names]
    for db, coll, kind in namespaces:
        metadata = json.dumps({"indexes": [], "collectionName": coll, "type": kind})
        data += bson({"db": db, "collection": coll, "metadata": metadata, "size": 0, "type": kind})
    data += TERMINATOR
    for db, coll, kind in namespaces:
        docs = databases.get(db, {}).get(coll, []) if kind == "collection" else []
        crc = 0
        data += bson({"db": db, "collection": coll, "EOF": False, "CRC": 0}, int64_keys=("CRC",))
        for document in docs:
            raw = bson(document)
            crc = crc64_ecma(raw, crc)
            data += raw
        data += TERMINATOR
        signed = struct.unpack("<q", struct.pack("<Q", crc))[0]
        data += bson({"db": db, "collection": coll, "EOF": True, "CRC": signed}, int64_keys=("CRC",)) + TERMINATOR
    return data


def read_archive_documents(data: bytes) -> dict[tuple[str, str], list[dict]] | None:
    """What mongorestore would read from ``data``; None when the archive is cut short."""
    if len(data) < 4 or struct.unpack_from("<I", data, 0)[0] != MONGO_ARCHIVE_MAGIC:
        raise ValueError("stream or file does not appear to be a mongodump archive")
    pos = 4

    def next_doc():
        nonlocal pos
        if pos + 4 > len(data):
            return None
        (length,) = struct.unpack_from("<i", data, pos)
        if length == -1:
            pos += 4
            return -1
        if pos + length > len(data):
            return None
        doc = data[pos : pos + length]
        pos += length
        return doc

    if next_doc() is None:
        return None
    namespaces: dict[tuple[str, str], list[dict]] = {}
    while True:
        doc = next_doc()
        if doc is None:
            return None
        if doc == -1:
            break
        meta = unbson(doc)
        namespaces[(meta["db"], meta["collection"])] = []
    closed = set()
    while pos < len(data):
        header = next_doc()
        if header is None or header == -1:
            return None
        info = unbson(header)
        ns = (info["db"], info["collection"])
        while True:
            doc = next_doc()
            if doc is None:
                return None
            if doc == -1:
                break
            namespaces.setdefault(ns, []).append(unbson(doc))
        if info["EOF"]:
            closed.add(ns)
    if set(namespaces) - closed:
        return None
    return namespaces


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

_AUTH_RE = re.compile(r"^db\.getSiblingDB\('admin'\)\.auth\((\".*?\"), (\".*?\")\);$", re.MULTILINE)
_ARGS_RE = re.compile(r"^const __arkive = (.*);$", re.MULTILINE)
SYSTEM_DBS = ("admin", "config", "local")


class FakeMongo(FakeDbContainer):
    """A mongod with databases of collections of documents (plus admin's users)."""

    def __init__(
        self,
        name: str = "mongo",
        databases: dict | None = None,
        user: str | None = "root",
        password: str | None = "mongo-secret",
        init_db: str | None = None,
    ):
        env = {}
        if user and password:
            env = {"MONGO_INITDB_ROOT_USERNAME": user, "MONGO_INITDB_ROOT_PASSWORD": password}
        if init_db:
            env["MONGO_INITDB_DATABASE"] = init_db
        super().__init__(name, env, "mongo:8.0")
        self.user, self.password = user, password
        self.databases: dict[str, dict[str, list[dict]]] = {
            "admin": {"system.users": [{"_id": f"admin.{user or 'none'}", "user": user or "none"}]}
        }
        for db_name, collections in (databases or {}).items():
            self.databases[db_name] = {coll: list(docs) for coll, docs in collections.items()}
        self.files: dict[str, bytes] = {}
        self.tmp_dirs: list[str] = []
        self.hello = {"ok": 1, "isWritablePrimary": True}
        # Failure switches.
        self.missing: set[str] = set()  # programs that are not in the image
        self.fail_restore_midway = False  # mongorestore dies after restoring the first collection
        self.failed_documents = 0  # mongorestore reports this many insert failures (exit 0)
        self.dump_truncated = False
        self.fail_drop = False
        self.fail_mktemp = False
        self.legacy_tools = False  # mongo:4.2 and older: no --config option

    # ---- helpers ----

    def _authenticated(self, user: str | None, password: str | None) -> bool:
        if self.user is None:
            return True
        return user == self.user and password == self.password

    def _flag(self, cmd: list[str], name: str) -> list[str]:
        """Values of ``--name=value`` and ``--name value`` (the dumper uses the second form)."""
        prefix = f"--{name}="
        values = [arg[len(prefix) :] for arg in cmd if arg.startswith(prefix)]
        values += [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == f"--{name}"]
        return values

    def _cli_password(self, cmd: list[str], stdin: bytes) -> str | None:
        if "--password" in cmd:
            return cmd[cmd.index("--password") + 1]
        config = self._flag(cmd, "config")
        if config:
            text = stdin.decode() if config[0] == "/dev/stdin" else self.files.get(config[0], b"").decode()
            match = re.match(r'^password: (".*")$', text.strip())
            return json.loads(match.group(1)) if match else None
        passwords = self._flag(cmd, "password")
        return passwords[0] if passwords else None

    def user_databases(self) -> list[str]:
        return sorted(n for n in self.databases if n not in SYSTEM_DBS)

    def documents(self, db: str, coll: str) -> list[dict]:
        return self.databases.get(db, {}).get(coll, [])

    # ---- commands ----

    def run(self, cmd, env, stdin):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        program = cmd[0]
        if program in self.missing or program not in (
            "mongosh",
            "mongodump",
            "mongorestore",
            "mktemp",
            "cp",
            "rm",
        ):
            return 127, b"", f'exec: "{program}": executable file not found in $PATH\n'.encode()
        if "--version" in cmd:
            version = "r4.2.24" if self.legacy_tools else "100.10.0"
            return 0, f"{program} version: {version}\n".encode(), b""
        if "--help" in cmd:
            # Database Tools 100.3+ list --config; the legacy 4.2 tools don't.
            text = "Usage:\n  mongodump <options> <connection-string>\n\nGeneral options:\n      --help\n"
            if not self.legacy_tools:
                text += "      --config=<filename>   path to a configuration file\n"
            return 0, text.encode(), b""
        if program == "mktemp":
            if self.fail_mktemp:
                return 1, b"", b"mktemp: failed to create directory: Read-only file system\n"
            folder = f"/tmp/arkive-restore.Ab{len(self.tmp_dirs)}Cd"
            self.tmp_dirs.append(folder)
            return 0, (folder + "\n").encode(), b""
        if program == "cp":
            assert cmd[1] == "/dev/stdin"
            self.files[cmd[2]] = stdin
            return 0, b"", b""
        if program == "rm":
            assert cmd[:3] == ["rm", "-rf", "--"]
            for path in list(self.files):
                if path.startswith(cmd[3] + "/"):
                    del self.files[path]
            self.tmp_dirs.remove(cmd[3])
            return 0, b"", b""
        if program == "mongosh":
            return self._mongosh(stdin.decode())
        user = (self._flag(cmd, "username") or [None])[0]
        if not self._authenticated(user, self._cli_password(cmd, stdin)):
            return (
                1,
                b"",
                b"Failed: can't create session: failed to connect to mongodb://localhost/: connection() error "
                b'occurred during connection handshake: auth error: unable to authenticate using mechanism "SCRAM-SHA-256"'
                b": (AuthenticationFailed) Authentication failed.\n",
            )
        if program == "mongodump":
            return self._mongodump(cmd)
        return self._mongorestore(cmd, stdin)

    def _mongosh(self, script: str):
        auth = _AUTH_RE.search(script)
        user, password = (json.loads(auth.group(1)), json.loads(auth.group(2))) if auth else (None, None)
        if auth and not self._authenticated(user, password):
            return 1, b"", b"MongoServerError: Authentication failed.\n"
        if not auth and self.user is not None:
            return 1, b"", b"MongoServerError: Command listDatabases requires authentication\n"
        args = json.loads(_ARGS_RE.search(script).group(1))
        if "estimatedDocumentCount" in script:
            result = {
                name: {coll: len(docs) for coll, docs in self.databases.get(name, {}).items()}
                for name in args["databases"]
            }
        elif "dropDatabase" in script:
            if self.fail_drop:
                return 1, b"", b"MongoServerError: not primary\n"
            names = args["targets"] if args["targets"] is not None else self.user_databases()
            for name in names:
                self.databases.pop(name, None)
            result = {"dropped": names}
        elif "listDatabases" in script and "writable" in script:
            result = {
                "version": "8.0.4",
                "msg": self.hello.get("msg"),
                "setName": self.hello.get("setName"),
                "writable": bool(self.hello.get("isWritablePrimary")),
                "databases": sorted(self.databases) + ["config", "local"],
            }
        else:
            return 1, b"", b"SyntaxError: unknown script\n"
        return 0, f"ARKIVE_JSON {json.dumps(result)}\n".encode(), b""

    def _mongodump(self, cmd):
        if self.legacy_tools and self._flag(cmd, "config"):
            return 2, b"", b'error parsing command line options: unknown option "config"\n'
        only = self._flag(cmd, "db")
        names = only if only else sorted(self.databases)
        data = mongo_archive({n: self.databases[n] for n in names if n in self.databases})
        if self.dump_truncated:
            data = data[: len(data) // 2]
        log = "".join(f"done dumping `{n}`\n" for n in names)
        return 0, data, log.encode()

    def _mongorestore(self, cmd, stdin):
        try:
            namespaces = read_archive_documents(stdin)
        except ValueError as exc:
            return (
                1,
                b"",
                f"Failed: {exc}\n0 document(s) restored successfully. 0 document(s) failed to restore.\n".encode(),
            )
        if namespaces is None:
            return (
                1,
                b"",
                b"Failed: corruption found in archive; ParserConsumer.BodyBSON() ( corruption found in archive; "
                b"read bson ( unexpected EOF ) )\n0 document(s) restored successfully. 0 document(s) failed to "
                b"restore.\n",
            )
        include = [p[:-2] for p in self._flag(cmd, "nsInclude")]
        exclude = [p[:-2] for p in self._flag(cmd, "nsExclude")]
        restored = 0
        for (db, coll), docs in namespaces.items():
            if include and db not in include:
                continue
            if db in exclude:
                continue
            collections = self.databases.setdefault(db, {})
            if "--drop" in cmd or coll not in collections:
                collections[coll] = []
            collections[coll].extend(docs)
            restored += len(docs)
            if self.fail_restore_midway:
                return 1, b"", b"Failed: app.notes: error restoring from archive: (NotWritablePrimary) not primary\n"
        failed = self.failed_documents
        summary = f"{restored} document(s) restored successfully. {failed} document(s) failed to restore.\n"
        return 0, b"", summary.encode()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def rdb_file(version: int = 11, keys: dict[str, str] | None = None, redis_ver: str = "7.2.4", checksum=True) -> bytes:
    """A small but complete RDB file (string keys in db 0)."""

    def string(value: str) -> bytes:
        raw = value.encode()
        assert len(raw) < 64
        return bytes([len(raw)]) + raw

    keys = {"session:1": "alice"} if keys is None else keys
    data = f"REDIS{version:04d}".encode()
    data += b"\xfa" + string("redis-ver") + string(redis_ver)
    data += b"\xfa" + string("ctime") + b"\xc2" + struct.pack("<i", 1793534400)
    data += b"\xfe\x00\xfb" + bytes([len(keys), 0])
    for key, value in keys.items():
        data += b"\x00" + string(key) + string(value)
    data += b"\xff"
    if version >= 5:
        data += struct.pack("<Q", crc64_jones(data) if checksum else 0)
    return data


class FakeRedis(FakeDbContainer):
    def __init__(
        self,
        name: str = "cache",
        version: str = "7.2.4",
        data_dir: str = "/data",
        mounts: list[dict] | None = None,
        appendonly: bool = False,
        password: str | None = None,
        image: str = "redis:7.2",
    ):
        env = {"REDIS_PASSWORD": password} if password else {}
        super().__init__(name, env, image)
        self.version = version
        self.config = {
            "dir": data_dir,
            "dbfilename": "dump.rdb",
            "appendonly": "yes" if appendonly else "no",
            "appendfilename": "appendonly.aof",
        }
        if tuple(int(p) for p in version.split(".")[:2]) >= (7, 0):
            self.config["appenddirname"] = "appendonlydir"
        self.password = password
        self.owner = "999:999"
        self.attrs["Mounts"] = (
            mounts
            if mounts is not None
            else [{"Type": "bind", "Source": "/mnt/user/appdata/redis", "Destination": data_dir}]
        )
        self.config_disabled = False

    def run(self, cmd, env, stdin):
        self.calls.append({"cmd": cmd, "env": env, "stdin": stdin})
        program = cmd[0]
        if program == "stat":
            return (0, f"{self.owner}\n".encode(), b"") if self.owner else (1, b"", b"stat: not found\n")
        if program != "redis-cli":
            return 127, b"", f'exec: "{program}": executable file not found in $PATH\n'.encode()
        if self.password and env.get("REDISCLI_AUTH") != self.password:
            return 0, b"NOAUTH Authentication required.\n", b""
        args = cmd[1:]
        if args == ["INFO", "server"]:
            return 0, f"# Server\r\nredis_version:{self.version}\r\nredis_mode:standalone\r\n".encode(), b""
        if args[:2] == ["CONFIG", "GET"]:
            if self.config_disabled:
                return 0, b"ERR unknown command 'CONFIG', with args beginning with: 'GET' 'dir' \n", b""
            key = args[2]
            if key not in self.config:
                return 0, b"\n", b""
            return 0, f"{key}\n{self.config[key]}\n".encode(), b""
        return 0, b"ERR unknown command\n", b""


class FakeDockerAPI(docker.APIClient):
    """The fake exec API behind an ``isinstance(api, docker.APIClient)`` check.

    Supports both ways DBDumper streams a dump: ``exec_start(stream=True,
    demux=True)`` (no stdin) and ``exec_start(socket=True)`` with stdin.
    """

    def __init__(self, container):  # noqa: D107 - no real Docker connection
        self._fake = FakeExecAPI(container)
        self.container = container

    def exec_create(self, container_id, cmd, **kwargs):
        return self._fake.exec_create(container_id, cmd, **kwargs)

    def exec_start(self, exec_id, socket=False, stream=False, demux=False, **kwargs):  # noqa: A002
        if socket:
            return self._fake.exec_start(exec_id, socket=True)
        record = self._fake.execs[exec_id]
        code, out, err = self.container.run(record["cmd"], record["env"], b"")
        record["exit_code"] = code
        chunks = [(out[i : i + 4096], None) for i in range(0, len(out), 4096)]
        if err:
            chunks.append((None, err))
        return iter(chunks)

    def exec_inspect(self, exec_id):
        return self._fake.exec_inspect(exec_id)

    @property
    def execs(self):
        return self._fake.execs


def with_docker_api(container):
    """Give a fake container an API client that passes DBDumper's real-client check."""
    container.client = SimpleNamespace(api=FakeDockerAPI(container))
    return container
