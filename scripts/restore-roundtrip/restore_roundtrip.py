#!/usr/bin/env python3
"""Real-engine restore round trip for Arkive.

Runs the full dump -> backup -> restore -> load cycle, plus a restore drill,
against real database servers and a real Arkive instance:

- Postgres 16 and 17 (several databases, a non-superuser database owner, a
  read-only role, extensions, large objects, partitions, a LATIN1 database,
  a database name with spaces, a non-default bootstrap superuser)
- MariaDB 11.4 and MySQL 8.4 (utf8mb4, binary BLOBs, latin1, a view, a
  trigger, a procedure, a function, an event, a second application database)
- SQLite in WAL mode held open by a running "app" (rows live only in the WAL)

Everything runs inside a throwaway Docker-in-Docker daemon, so the host's
containers are never discovered, dumped or swept by the Arkive under test,
and the drill's throwaway containers never appear on the host. Requires a
host Docker that can run a privileged ``docker:dind`` container and an Arkive
image (``--image``; build one with ``docker build -t arkive-rt:dev .``).

    python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev
    python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev --live-code

``--live-code`` mounts this checkout's backend/ over the image's, so backend
changes are tested without rebuilding the image. Exit status is 0 only when
every check passed. ``--keep`` leaves the environment running for inspection
(remove it later with ``--down``).

Every container, file and port it uses is derived from ``--prefix``
(default ``arkive-rt``) and ``--port`` (default 8320); state lives in
``--workdir`` (default ``/tmp/<prefix>``).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXTURES_IN_DIND = "/src/scripts/restore-roundtrip/fixtures"

DIND_IMAGE = "docker:29-dind"
PG_IMAGES = {"pg16": "postgres:16", "pg17": "postgres:17"}
MYSQL_IMAGES = {"mariadb": "mariadb:11.4", "mysql": "mysql:8.4"}
NODB_KEY = "mariadb-nodb"  # MariaDB without MARIADB_DATABASE
SQLITE_APP_IMAGE = "python:3.12-alpine"

# Inside the DinD daemon's filesystem (all under the bind-mounted workdir).
APPDATA = "/mnt/user/appdata"
ARKIVE_CONFIG = f"{APPDATA}/arkive-rt"
SQLITE_APP_DATA = f"{APPDATA}/sqliteapp"
BACKUPS = "/mnt/backups"
ENCRYPTION_PASSWORD = "roundtrip-encryption-password"

# Bootstrap users of the two Postgres clusters: pg17 deliberately does not
# have a "postgres" role (like an Immich-style POSTGRES_USER=immich setup).
PG_USERS = {"pg16": "postgres", "pg17": "immich"}
MYSQL_CLIENTS = {"mariadb": "mariadb", "mysql": "mysql"}
MYSQL_ROOT_ENV = {"mariadb": "MARIADB_ROOT_PASSWORD", "mysql": "MYSQL_ROOT_PASSWORD"}

PG_CHECKS = {
    "appdb": {
        "notes": "SELECT count(*), md5(string_agg(id || body || coalesce(tags::text, '') || coalesce(email::text, '') "
        "|| uid || coalesce(doc::text, '') || coalesce(encode(raw, 'hex'), '') || coalesce(amounts::text, '') "
        "|| created, '|' ORDER BY id)) FROM notes",
        "partitions": "SELECT count(*), md5(string_agg(id || '/' || at || '/' || payload, '|' ORDER BY id)) FROM events",
        "large objects": "SELECT count(*), md5(string_agg(name || md5(lo_get(content)), '|' ORDER BY id)) FROM attachments",
        "extensions": "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension",
        "functions": "SELECT string_agg(proname, ',' ORDER BY proname) FROM pg_proc "
        "WHERE pronamespace = 'public'::regnamespace AND proname IN ('touch', 'ensure_audit_log')",
        "triggers": "SELECT string_agg(tgname, ',') FROM pg_trigger WHERE NOT tgisinternal",
        "materialized view": "SELECT n FROM note_counts",
        "comment": "SELECT obj_description('notes'::regclass)",
        "schema": "SELECT string_agg(k || v, ',') FROM reporting.summary",
        "reader grant": "SELECT has_table_privilege('app_reader', 'notes', 'SELECT')",
    },
    "shopdb": {
        "products": "SELECT count(*), md5(string_agg(id || name || price, '|' ORDER BY id)) FROM products",
        "orders": "SELECT count(*), md5(string_agg(id || '-' || product_id || '-' || qty, '|' ORDER BY id)) FROM orders",
        "table owners": "SELECT string_agg(DISTINCT tableowner, ',') FROM pg_tables WHERE schemaname = 'public'",
        "database owner": "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
    },
    "Mixed Case DB": {
        "table": 'SELECT count(*), md5(string_agg(id || "Value", \'|\' ORDER BY id)) FROM "Weird Table"',
    },
    "latin1db": {
        "encoding": "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()",
        "text": "SELECT string_agg(w, ',' ORDER BY w) FROM words",
    },
    "postgres": {
        "role settings": "SELECT string_agg(r.rolname || ':' || array_to_string(s.setconfig, ';'), ',') "
        "FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole",
        "login roles": "SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles "
        "WHERE rolcanlogin AND rolname LIKE 'app_%'",
    },
}

# Things the application roles must still be able to do after a restore.
PG_ROLE_CHECKS = [
    ("app_owner", "owner-pass", "shopdb", "INSERT INTO products (name, price) VALUES ('after restore', 1) RETURNING 1"),
    ("app_reader", "reader-pass", "shopdb", "SELECT count(*) > 0 FROM orders"),
    ("app_reader", "reader-pass", "appdb", "SELECT count(*) > 0 FROM notes"),
]

MYSQL_CHECKS = {
    "files": "SELECT COUNT(*), SUM(CRC32(CONCAT_WS('|', id, HEX(name), HEX(IFNULL(data, '')), "
    "IFNULL(meta, ''), created))) FROM appdb.files",
    "trigger rows": "SELECT COUNT(*), SUM(CRC32(CONCAT_WS('|', id, file_id, action))) FROM appdb.file_log",
    "latin1 text": "SELECT GROUP_CONCAT(HEX(s) ORDER BY s) FROM appdb.legacy",
    "routines": "SELECT GROUP_CONCAT(ROUTINE_NAME ORDER BY ROUTINE_NAME) FROM information_schema.ROUTINES "
    "WHERE ROUTINE_SCHEMA = 'appdb'",
    "events": "SELECT GROUP_CONCAT(EVENT_NAME) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = 'appdb'",
    "triggers": "SELECT GROUP_CONCAT(TRIGGER_NAME) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'appdb'",
    "views": "SELECT GROUP_CONCAT(TABLE_NAME) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = 'appdb'",
    "second database": "SELECT GROUP_CONCAT(HEX(title) ORDER BY id) FROM photos.albums",
}

SQLITE_CHECK = r"""
import json, sqlite3, sys
out = {}
for name, queries in {
    "app.db": {"integrity": "PRAGMA integrity_check", "items": "SELECT count(*), sum(length(payload)) FROM items",
               "ticks": "SELECT count(*) > 0 FROM ticks"},
    "other.sqlite3": {"integrity": "PRAGMA integrity_check", "theme": "SELECT v FROM settings WHERE k = 'theme'"},
}.items():
    path = sys.argv[1] + "/" + name if not sys.argv[1].endswith((".db", ".sqlite3")) else sys.argv[1]
    if len(sys.argv) > 2:
        path = sys.argv[2] if name == "app.db" else sys.argv[3]
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        out[name] = {k: [list(r) for r in conn.execute(q).fetchall()] for k, q in queries.items()}
    except Exception as exc:
        out[name] = {"error": str(exc)}
print(json.dumps(out))
"""


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        (self.passed if ok else self.failed).append(f"{label}{': ' + detail if detail else ''}")
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + detail if detail and not ok else ''}", flush=True)
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  [NOTE] {text}", flush=True)


class Harness:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.prefix = args.prefix
        self.port = args.port
        self.workdir = Path(args.workdir or f"/tmp/{self.prefix}")
        self.sock = self.workdir / "run" / "docker.sock"
        self.api_key: str | None = None
        self.report = Report()
        self.expected_databases: list[tuple[str, str]] = []

    # ---- docker helpers ----

    def name(self, suffix: str) -> str:
        return f"{self.prefix}-{suffix}"

    @staticmethod
    def host(*args: str, check: bool = True, capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True, **kwargs)

    def dind(self, *args: str, check: bool = True, input: str | bytes | None = None, text: bool = True):
        return subprocess.run(
            ["docker", "-H", f"unix://{self.sock}", *args],
            check=check,
            capture_output=True,
            input=input,
            text=text,
        )

    def sh(self, container: str, script: str, env: dict | None = None, check: bool = True):
        env_args = [arg for key, value in (env or {}).items() for arg in ("-e", f"{key}={value}")]
        return self.dind("exec", *env_args, self.name(container), "sh", "-c", script, check=check)

    def wait_for(self, what: str, probe, timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe():
                return
            time.sleep(2)
        raise RuntimeError(f"timed out waiting for {what}")

    # ---- environment ----

    def up(self) -> None:
        print(f"== starting Docker-in-Docker ({self.name('dind')}, state in {self.workdir})", flush=True)
        for sub in ("docker", "run", "mnt-user", "backups"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        self.host("rm", "-f", self.name("dind"), check=False)
        self.host(
            "run", "-d", "--name", self.name("dind"), "--privileged",
            "--memory", self.args.memory,
            "-e", "DOCKER_TLS_CERTDIR=",
            "-v", f"{self.workdir}/docker:/var/lib/docker",
            "-v", f"{self.workdir}/run:/var/run",
            "-v", f"{self.workdir}/mnt-user:/mnt/user",
            "-v", f"{self.workdir}/backups:{BACKUPS}",
            "-v", f"{REPO}:/src:ro",
            "-p", f"127.0.0.1:{self.port}:{self.port}",
            DIND_IMAGE, "dockerd", "--host=unix:///var/run/docker.sock",
        )  # fmt: skip
        self.wait_for("the DinD daemon", lambda: self.dind("info", check=False).returncode == 0, 60)

        images = [*PG_IMAGES.values(), *MYSQL_IMAGES.values(), SQLITE_APP_IMAGE]
        for image in images:
            local = self.args.load_local_images and self.host("image", "inspect", image, check=False).returncode == 0
            if local:  # reuse the host's copy instead of pulling again
                save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
                subprocess.run(["docker", "-H", f"unix://{self.sock}", "load", "-q"], stdin=save.stdout, check=True)
                save.wait()
            else:
                self.dind("pull", "-q", image)
        print(f"== loading {self.args.image} into DinD", flush=True)
        save = subprocess.Popen(["docker", "save", self.args.image], stdout=subprocess.PIPE)
        subprocess.run(["docker", "-H", f"unix://{self.sock}", "load", "-q"], stdin=save.stdout, check=True)
        if save.wait() != 0:
            raise RuntimeError(f"docker save {self.args.image} failed")
        self.start_fixtures()
        self.start_arkive()

    def start_fixtures(self) -> None:
        print("== starting database fixtures", flush=True)
        fixtures = f"{FIXTURES_IN_DIND}:/fixtures:ro"
        for key, image in PG_IMAGES.items():
            self.dind(
                "run", "-d", "--name", self.name(key), "-e", f"POSTGRES_PASSWORD={key}-pass",
                "-e", f"POSTGRES_USER={PG_USERS[key]}", "-e", "POSTGRES_DB=appdb", "-v", fixtures, image,
            )  # fmt: skip
        for key, image in MYSQL_IMAGES.items():
            prefix = "MARIADB" if key == "mariadb" else "MYSQL"
            self.dind(
                "run", "-d", "--name", self.name(key), "-e", f"{prefix}_ROOT_PASSWORD={key}-root",
                "-e", f"{prefix}_DATABASE=appdb", "-e", f"{prefix}_USER=app", "-e", f"{prefix}_PASSWORD=app-pass",
                "-v", fixtures, image,
            )  # fmt: skip
        # No MARIADB_DATABASE: databases created by hand later (a common shared
        # MariaDB setup). Arkive dumps its "mysql" system schema plus every
        # application database it can list.
        self.dind(
            "run", "-d", "--name", self.name(NODB_KEY), "-e", f"MARIADB_ROOT_PASSWORD={NODB_KEY}-root",
            MYSQL_IMAGES["mariadb"],
        )  # fmt: skip
        self.dind(
            "run", "-d", "--name", self.name("sqliteapp"), "-v", f"{SQLITE_APP_DATA}:/data", "-v", fixtures,
            SQLITE_APP_IMAGE, "python", "/fixtures/sqlite_app.py", "/data",
        )  # fmt: skip

        for key, user in PG_USERS.items():
            self.wait_for(key, lambda k=key: self.pg_ready(k))
            psql = f"psql -X -q -v ON_ERROR_STOP=1 -U {user} -d appdb"
            self.sh(key, f"{psql} -f /fixtures/postgres_cluster.sql && {psql} -f /fixtures/postgres_appdb.sql")
            self.sh(
                key,
                "psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U app_owner -d shopdb -f /fixtures/postgres_shopdb.sql",
                env={"PGPASSWORD": "owner-pass"},
            )
            self.sh(key, f"{psql} -f /fixtures/postgres_misc.sql")
        for key in MYSQL_IMAGES:
            self.wait_for(key, lambda k=key: self.mysql_ready(k), 240)
            client = MYSQL_CLIENTS[key]
            self.sh(key, f"{client} -uroot --default-character-set=utf8mb4 < /fixtures/mysql.sql",
                    env={"MYSQL_PWD": f"{key}-root"})  # fmt: skip
        self.wait_for(NODB_KEY, lambda: self.mysql_ready(NODB_KEY), 240)
        self.sh(
            NODB_KEY,
            'mariadb -uroot --default-character-set=utf8mb4 -e "CREATE DATABASE wiki; '
            "CREATE TABLE wiki.pages (id INT PRIMARY KEY, title VARCHAR(50)) DEFAULT CHARSET=utf8mb4; "
            "INSERT INTO wiki.pages VALUES (1, 'Home ✓'), (2, 'About');\"",
            env={"MYSQL_PWD": f"{NODB_KEY}-root"},
        )
        self.wait_for(
            "the SQLite app",
            lambda: (self.workdir / "mnt-user" / SQLITE_APP_DATA.removeprefix("/mnt/user/") / "ready").exists(),
            60,
        )

    def pg_ready(self, key: str) -> bool:
        return self.sh(key, "pg_isready -q -h 127.0.0.1", check=False).returncode == 0

    def mysql_ready(self, key: str, password: str | None = None) -> bool:
        result = self.sh(
            key,
            '"$(command -v mariadb || command -v mysql)" -uroot -h127.0.0.1 --protocol=tcp -e "select 1"',
            env={"MYSQL_PWD": password or f"{key}-root"},
            check=False,
        )
        return result.returncode == 0

    def start_arkive(self) -> None:
        print("== starting Arkive", flush=True)
        args = [
            "run", "-d", "--name", self.name("arkive"), "--user", "0:0",
            "-e", "TZ=UTC",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", "/mnt/user:/mnt/user:ro",
            "-v", f"{ARKIVE_CONFIG}:/config",
            "-v", f"{BACKUPS}:{BACKUPS}",
            "-p", f"{self.port}:{self.port}",
        ]  # fmt: skip
        if self.args.live_code:
            args += ["-v", "/src/backend:/app/backend:ro"]
        args += [
            self.args.image, "python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
            "--port", str(self.port), "--workers", "1", "--log-config", "/app/backend/logging.json",
        ]  # fmt: skip
        self.dind(*args)
        self.wait_for("the Arkive API", lambda: self.api("GET", "/api/status", auth=False, check=False) is not None)

    def down(self) -> None:
        print(f"== removing {self.name('dind')} and {self.workdir}", flush=True)
        self.host("rm", "-f", "-v", self.name("dind"), check=False)
        if self.workdir.is_dir():
            # Files created by root inside DinD: remove them from a container.
            self.host("run", "--rm", "-v", f"{self.workdir}:/w", "alpine:3.20", "sh", "-c", "rm -rf /w/*", check=False)
            shutil.rmtree(self.workdir, ignore_errors=True)

    # ---- Arkive API ----

    def api(
        self, method: str, path: str, body: dict | None = None, auth: bool = True, check: bool = True, raw: bool = False
    ):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        if auth and self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            if check:
                detail = exc.read().decode(errors="replace") if isinstance(exc, urllib.error.HTTPError) else exc
                raise RuntimeError(f"{method} {path} failed: {detail}") from exc
            return None
        if raw:
            return payload
        return json.loads(payload) if payload else {}

    def setup_arkive(self) -> tuple[str, list[dict]]:
        if not self.api_key:  # --reuse keeps the instance set up by an earlier run
            result = self.api(
                "POST",
                "/api/auth/setup",
                {
                    "encryption_password": ENCRYPTION_PASSWORD,
                    "storage": {"type": "local", "name": "Local", "path": f"{BACKUPS}/repo"},
                    "directories": [SQLITE_APP_DATA],
                },
                auth=False,
            )
            self.api_key = result["api_key"]
            if result.get("target_errors"):
                raise RuntimeError(f"target errors: {result['target_errors']}")
            (self.workdir / "api_key").write_text(self.api_key)
        targets = self.api("GET", "/api/targets")
        targets = targets.get("items", targets.get("targets", targets)) if isinstance(targets, dict) else targets
        jobs = self.api("GET", "/api/jobs")
        jobs = jobs.get("items", jobs.get("jobs", jobs)) if isinstance(jobs, dict) else jobs
        return targets[0]["id"], jobs

    # ---- source fingerprints ----

    def pg_query(self, key: str, database: str, sql: str, user: str | None = None, password: str | None = None):
        user = user or PG_USERS.get(key.split("-")[0], "postgres")
        args = ["psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database, "-c", sql]
        env = []
        if password:
            args[1:1] = ["-h", "127.0.0.1"]
            env = ["-e", f"PGPASSWORD={password}"]
        result = self.dind("exec", *env, self.name(key), *args, check=False)
        return result.stdout.strip() if result.returncode == 0 else f"ERROR: {result.stderr.strip()[-300:]}"

    def mysql_query(self, key: str, sql: str, password: str) -> str:
        result = self.dind(
            "exec", "-e", f"MYSQL_PWD={password}", self.name(key), "sh", "-c",
            '"$(command -v mariadb || command -v mysql)" -uroot -N -B --default-character-set=utf8mb4 -e "$0"', sql,
            check=False,
        )  # fmt: skip
        return result.stdout.strip() if result.returncode == 0 else f"ERROR: {result.stderr.strip()[-300:]}"

    def mysql_fingerprint(self, key: str, password: str) -> dict:
        return {label: self.mysql_query(key, sql, password) for label, sql in MYSQL_CHECKS.items()}

    def sqlite_fingerprint(self, container_path_app: str, container_path_other: str, mount: str) -> dict:
        result = self.dind(
            "run", "--rm", "-v", mount, SQLITE_APP_IMAGE, "python", "-c", SQLITE_CHECK, "-",
            container_path_app, container_path_other,
        )  # fmt: skip
        return json.loads(result.stdout)

    # ---- the round trip ----

    def run_backup(self, jobs: list[dict]) -> dict:
        job = next(j for j in jobs if j["type"] == "full")
        print(f"== running backup job {job['name']} ({job['id']})", flush=True)
        started = self.api("POST", f"/api/jobs/{job['id']}/run")
        run_id = started["run_id"]
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            run = self.api("GET", f"/api/jobs/runs/{run_id}")
            if run.get("status") not in ("running", "pending", "queued"):
                return run
            time.sleep(3)
        raise RuntimeError("backup did not finish within 30 minutes")

    def check_backup(self, run: dict) -> None:
        r = self.report
        print(f"== backup run {run['id']}: {run['status']} {run.get('error_message') or ''}", flush=True)
        r.check(
            run["status"] == "success", "backup run status is success", f"{run['status']}: {run.get('error_message')}"
        )
        dumped = {(d["container_name"], d["db_name"]): d for d in run.get("databases", [])}
        expected = [(self.name(k), db) for k in PG_IMAGES for db in ("appdb", "shopdb", "Mixed Case DB", "latin1db")]
        expected += [(self.name(k), db) for k in MYSQL_IMAGES for db in ("appdb", "photos")]
        expected += [(self.name(NODB_KEY), "mysql"), (self.name(NODB_KEY), "wiki")]
        expected += [(self.name("sqliteapp"), "app.db"), (self.name("sqliteapp"), "other.sqlite3")]
        self.expected_databases = expected
        for key in expected:
            entry = dumped.get(key)
            ok = entry is not None and entry.get("status") == "success"
            r.check(ok, f"dumped {key[0]}/{key[1]}", "not discovered" if entry is None else str(entry.get("error")))
        for (container, db_name), entry in sorted(dumped.items()):
            if (container, db_name) not in expected and entry.get("status") != "success":
                r.check(False, f"dump {container}/{db_name}", str(entry.get("error")))

    def restore_dumps(self, target_id: str, run: dict) -> Path:
        snapshot_id = next((t["snapshot_id"] for t in run.get("targets", []) if t.get("snapshot_id")), None)
        if not snapshot_id:
            raise RuntimeError(f"backup run recorded no snapshot: {run.get('targets')}")
        name = f"roundtrip-{run['id']}"
        restore_to = f"/config/restores/{name}"
        print(f"== restoring /config/dumps from snapshot {snapshot_id[:8]} via the API", flush=True)
        result = self.api(
            "POST",
            "/api/restore",
            {"snapshot_id": snapshot_id, "target": target_id, "paths": ["/config/dumps"], "restore_to": restore_to},
        )
        self.report.check(result.get("status") == "success", "restore API", json.dumps(result)[:300])
        return Path(f"{ARKIVE_CONFIG}/restores/{name}/config/dumps")

    def manifest(self, dumps_dir: Path) -> list[dict]:
        local = self.workdir / "mnt-user" / str(dumps_dir).removeprefix("/mnt/user/") / "arkive-dumps-manifest.json"
        return json.loads(local.read_text())["dumps"]

    @staticmethod
    def load_errors(stderr: str) -> list[str]:
        benign = ("already exists",)
        return [
            line.strip()
            for line in stderr.splitlines()
            if "ERROR" in line and not any(marker in line for marker in benign)
        ]

    def restore_plan(self) -> list[dict]:
        """Database restore steps of the restore plan Arkive generates.

        Section 3 of the plan lists (container, type, database) and section 4
        step 5 the matching commands, in the same order. Only the commands for
        a plain dump are kept (the plan also shows gzip and legacy variants).
        """
        self.api("GET", "/api/restore/plan/pdf", raw=True)  # also writes restore-plan.html
        page = (self.workdir / "mnt-user" / ARKIVE_CONFIG.removeprefix("/mnt/user/") / "restore-plan.html").read_text()
        # Cell contents are HTML-escaped, so [^<]* never runs into the next cell.
        rows = re.findall(r"<tr>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td><code>", page)
        steps = re.findall(r"<p><strong>([^<]*) \((\w+)\):</strong></p>\s*<pre>([^<]*)</pre>", page)
        if len(rows) != len(steps):
            raise RuntimeError(f"restore plan lists {len(rows)} databases but {len(steps)} restore steps")
        plan = []
        for (container, db_type, db_name), (_c, _t, block) in zip(rows, steps):
            commands = []
            for line in html.unescape(block).splitlines():
                if line.startswith(("# gzip", "# dumps from")):
                    break
                if line.strip() and not line.startswith("#"):
                    commands.append(line)
            plan.append(
                {
                    "container": html.unescape(container),
                    "db_type": html.unescape(db_type),
                    "db_name": html.unescape(db_name),
                    "commands": commands,
                }
            )
        return plan

    def run_plan_step(self, step: dict, replacements: dict[str, str], dumps_dir: Path):
        """Run a plan step in the DinD daemon (it has a docker CLI) from the
        restored dump directory, pointed at scratch containers/paths."""
        script = "\n".join(step["commands"])
        for old, new in replacements.items():
            script = re.sub(rf"(?<![\w/.-]){re.escape(old)}(?![\w-])", new, script)
        return self.host(
            "exec", self.name("dind"), "sh", "-ec", f"cd {shlex.quote(str(dumps_dir))}\n{script}", check=False
        )

    def verify_postgres(self, key: str, dumps_dir: Path, entries: list[dict], plan: list[dict], source: dict):
        r = self.report
        container = self.name(key)
        scratch = self.name(f"{key}-restored")
        user = PG_USERS[key]
        self.dind("rm", "-f", scratch, check=False)
        self.dind(
            "run", "-d", "--name", scratch, "-e", "POSTGRES_PASSWORD=restored-pass", "-e", f"POSTGRES_USER={user}",
            "-e", "POSTGRES_DB=appdb", PG_IMAGES[key],
        )  # fmt: skip
        self.wait_for(scratch, lambda: self.pg_ready(f"{key}-restored"))
        ours = [e for e in entries if e["container_name"] == container]
        r.check(any(e.get("kind") == "globals" for e in ours), f"{key}: roles dump present")
        steps = [s for s in plan if s["container"] == container]
        planned = {s["db_name"] for s in steps}
        for entry in ours:
            label = "roles (pg_dumpall --globals-only)" if entry.get("kind") == "globals" else entry["db_name"]
            r.check(label in planned, f"{key}: restore plan covers {entry['db_name']}", str(sorted(planned)))
        for step in steps:  # plan order: roles first
            loaded = self.run_plan_step(step, {container: scratch}, dumps_dir)
            errors = self.load_errors(loaded.stderr)
            r.check(
                loaded.returncode == 0 and not errors,
                f"{key}: restore plan step for {step['db_name']} runs without errors",
                "; ".join(errors[:3]) or loaded.stderr.strip()[-300:],
            )
        restored = self.pg_fingerprint(f"{key}-restored", user)
        for label, value in source.items():
            r.check(
                restored.get(label) == value, f"{key}: {label}", f"source={value!r} restored={restored.get(label)!r}"
            )
        for role, password, database, sql in PG_ROLE_CHECKS:
            value = self.pg_query(f"{key}-restored", database, sql, user=role, password=password)
            # psql -tA prints the RETURNING row, then the command tag ("INSERT 0 1").
            first = value.splitlines()[0] if value else ""
            r.check(first in ("t", "1"), f"{key}: {role} can still use {database}", value)

    def pg_fingerprint(self, container_key: str, user: str) -> dict:
        out = {}
        for database, checks in PG_CHECKS.items():
            for label, sql in checks.items():
                out[f"{database}: {label}"] = self.pg_query(container_key, database, sql, user=user)
        return out

    def verify_mysql(self, key: str, dumps_dir: Path, entries: list[dict], plan: list[dict], source: dict):
        r = self.report
        container = self.name(key)
        scratch_key = f"{key}-restored"
        scratch = self.name(scratch_key)
        prefix = "MARIADB" if key == "mariadb" else "MYSQL"
        self.dind("rm", "-f", scratch, check=False)
        self.dind(
            "run", "-d", "--name", scratch, "-e", f"{prefix}_ROOT_PASSWORD=restored-root", MYSQL_IMAGES[key],
        )  # fmt: skip
        self.wait_for(scratch, lambda: self.mysql_ready(scratch_key, "restored-root"), 240)
        steps = [s for s in plan if s["container"] == container]
        planned = {s["db_name"] for s in steps}
        for entry in [e for e in entries if e["container_name"] == container]:
            r.check(entry["db_name"] in planned, f"{key}: restore plan covers {entry['db_name']}", str(sorted(planned)))
        for step in steps:
            loaded = self.run_plan_step(step, {container: scratch}, dumps_dir)
            r.check(
                loaded.returncode == 0,
                f"{key}: restore plan step for {step['db_name']} runs without errors",
                loaded.stderr.strip()[-300:],
            )
        restored = self.mysql_fingerprint(scratch_key, "restored-root")
        for label, value in source.items():
            r.check(
                restored.get(label) == value, f"{key}: {label}", f"source={value!r} restored={restored.get(label)!r}"
            )

    def verify_sqlite(self, dumps_dir: Path, entries: list[dict], plan: list[dict]) -> None:
        """Follow the plan's SQLite steps against a scratch copy of the app's
        directory that still holds a stale write-ahead log from a different
        database (an app that crashed): the restored file must win."""
        r = self.report
        container = self.name("sqliteapp")
        scratch = self.name("sqlite-restored")
        scratch_dir = f"{APPDATA}/sqlite-restored"
        files = {e["db_name"]: e["file"] for e in entries if e.get("db_type") == "sqlite"}
        if "app.db" not in files or "other.sqlite3" not in files:
            r.check(False, "sqlite: both databases in the backup", str(sorted(files)))
            return
        self.dind("rm", "-f", scratch, check=False)
        stale = (
            "import os, sqlite3\n"
            "c = sqlite3.connect('/d/app.db', isolation_level=None)\n"
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')\n"
            "c.execute('CREATE TABLE stale_marker (x)'); c.execute('INSERT INTO stale_marker VALUES (1)')\n"
            "os._exit(0)\n"
        )
        self.dind("run", "--rm", "-v", f"{scratch_dir}:/d", SQLITE_APP_IMAGE, "python", "-c", stale)
        self.dind("run", "-d", "--name", scratch, "-v", f"{scratch_dir}:/d", SQLITE_APP_IMAGE, "sleep", "infinity")
        steps = [s for s in plan if s["container"] == container and s["db_type"] == "sqlite"]
        r.check({s["db_name"] for s in steps} >= {"app.db", "other.sqlite3"}, "sqlite: restore plan covers both")
        for step in steps:
            loaded = self.run_plan_step(step, {container: scratch, SQLITE_APP_DATA: scratch_dir}, dumps_dir)
            r.check(
                loaded.returncode == 0,
                f"sqlite: restore plan step for {step['db_name']} runs without errors",
                loaded.stderr.strip()[-300:],
            )
        restored = self.sqlite_fingerprint("/d/app.db", "/d/other.sqlite3", f"{scratch_dir}:/d")
        app, other = restored.get("app.db", {}), restored.get("other.sqlite3", {})
        r.check(app.get("integrity") == [["ok"]], "sqlite: app.db integrity", str(app))
        r.check(app.get("items") == [[2000, 2000 * 1024]], "sqlite: WAL-only rows restored", str(app))
        r.check(app.get("ticks") == [[1]], "sqlite: rows written while running restored", str(app.get("ticks")))
        r.check(other.get("theme") == [["dark"]], "sqlite: rollback-journal database restored", str(other))

    def run_drill(self) -> None:
        r = self.report
        print("== running a restore drill", flush=True)
        self.api("PUT", "/api/settings", {"restore_drill_max_databases": "50"})
        drill_id = self.api("POST", "/api/drills/run")["drill_id"]
        deadline = time.monotonic() + 3600
        drill = {}
        while time.monotonic() < deadline:
            drill = self.api("GET", f"/api/drills/{drill_id}")
            if drill.get("status") != "running":
                break
            time.sleep(5)
        print(f"   drill {drill_id}: {drill.get('status')} {drill.get('error_message') or ''}", flush=True)
        results = drill.get("results") or []
        for result in results:
            label = f"drill {result['container_name']}/{result.get('db_name') or result.get('dump_file')}"
            if result["status"] == "skipped":
                r.note(f"{label} skipped: {result.get('detail')}")
                continue
            r.check(result["status"] == "passed", label, result.get("detail", ""))
        tested = {(x["container_name"], x.get("db_name")) for x in results if x["status"] in ("passed", "failed")}
        missing = sorted(set(self.expected_databases) - tested)
        r.check(not missing, "drill tested every database", f"not tested: {missing}")
        r.check(drill.get("status") == "passed", "drill status is passed", str(drill.get("error_message")))

    def remove_scratch(self) -> None:
        """Drop the servers the restore was loaded into (a later --reuse run
        would otherwise discover and back them up)."""
        names = [self.name(f"{key}-restored") for key in [*PG_IMAGES, *MYSQL_IMAGES]] + [self.name("sqlite-restored")]
        self.dind("rm", "-f", "-v", *names, check=False)

    def roundtrip(self) -> int:
        self.remove_scratch()
        target_id, jobs = self.setup_arkive()
        print("== recording source fingerprints", flush=True)
        pg_source = {key: self.pg_fingerprint(key, user) for key, user in PG_USERS.items()}
        mysql_source = {key: self.mysql_fingerprint(key, f"{key}-root") for key in MYSQL_IMAGES}
        run = self.run_backup(jobs)
        (self.workdir / "backup-run.json").write_text(json.dumps(run, indent=2))
        self.check_backup(run)
        dumps_dir = self.restore_dumps(target_id, run)
        entries = self.manifest(dumps_dir)
        plan = self.restore_plan()
        (self.workdir / "restore-plan-steps.json").write_text(json.dumps(plan, indent=2))
        print("== following the restore plan into fresh servers", flush=True)
        for key in PG_IMAGES:
            self.verify_postgres(key, dumps_dir, entries, plan, pg_source[key])
        for key in MYSQL_IMAGES:
            self.verify_mysql(key, dumps_dir, entries, plan, mysql_source[key])
        self.verify_sqlite(dumps_dir, entries, plan)
        self.remove_scratch()
        self.run_drill()
        r = self.report
        print(f"\n== {len(r.passed)} passed, {len(r.failed)} failed, {len(r.notes)} notes", flush=True)
        for line in r.failed:
            print(f"   FAIL {line}")
        return 1 if r.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default="arkive-rt:dev", help="Arkive image to test (must exist on the host)")
    parser.add_argument("--prefix", default="arkive-rt", help="name prefix of everything created")
    parser.add_argument("--port", type=int, default=8320, help="127.0.0.1 port for the Arkive API")
    parser.add_argument("--workdir", help="state directory (default /tmp/<prefix>)")
    parser.add_argument("--memory", default="12g", help="memory limit of the DinD container")
    parser.add_argument("--live-code", action="store_true", help="mount this checkout's backend/ into the image")
    parser.add_argument(
        "--load-local-images", action="store_true", help="copy fixture images from the host instead of pulling them"
    )
    parser.add_argument("--keep", action="store_true", help="leave the environment running")
    parser.add_argument("--reuse", action="store_true", help="use an environment left by --keep (skip setup)")
    parser.add_argument("--down", action="store_true", help="only remove an environment left by --keep")
    args = parser.parse_args()
    if not shutil.which("docker"):
        print("docker CLI not found", file=sys.stderr)
        return 2
    harness = Harness(args)
    if args.down:
        harness.down()
        return 0
    try:
        if not args.reuse:
            harness.up()
        else:
            key_file = harness.workdir / "api_key"
            harness.api_key = key_file.read_text().strip() if key_file.exists() else None
            # Pick up backend changes when the image runs with --live-code.
            harness.dind("restart", harness.name("arkive"))
            harness.wait_for("the Arkive API", lambda: harness.api("GET", "/api/status", check=False) is not None)
        return harness.roundtrip()
    except Exception as exc:
        print(f"round trip aborted: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(shlex.join(exc.cmd), exc.stderr, file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            harness.down()


if __name__ == "__main__":
    sys.exit(main())
