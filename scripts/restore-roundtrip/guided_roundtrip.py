#!/usr/bin/env python3
"""Real-engine round trip of guided database restore for MongoDB and Redis.

For each fixture: write data (state A), run Arkive's backup job, change or
destroy the data (state B), restore it with a guided restore and check that
state A is back, then undo and check that state B is back.

- MongoDB 8.0 with a root login and MONGO_INITDB_DATABASE (one database is
  restored; another database on the same server must stay as it is), MongoDB
  8.0 without a login (the whole server: every database except admin, config
  and local), and a single-node MongoDB 7.0 replica set. Restored in place
  through the API, then undone through the API.
- Redis 7.4 with the append-only file and a password, Redis 8.0 without it,
  Redis 6.2 with the (single-file) append-only file. Arkive prepares the
  file; the harness runs the exact steps Arkive returns, restarts Redis once
  more to prove the data was persisted the way Redis will load it next time,
  then runs the same steps with the safety copy (the documented way back).

Like restore_roundtrip.py, everything runs inside a throwaway Docker-in-Docker
daemon; unlike it, no port is published on the host: the API is called from
inside the Arkive container. Use the same ``--workdir`` ownership marker rules
(see restore_roundtrip.py and the README).

    python3 scripts/restore-roundtrip/guided_roundtrip.py --image arkive-rt:dev --live-code
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from restore_roundtrip import APPDATA, BACKUPS, DIND_IMAGE, ENCRYPTION_PASSWORD, Harness  # noqa: E402

MONGO_PASSWORD = 'p@ss w"rd $1'
REDIS_PASSWORD = "rt-redis-secret"
NOTES_DIR = f"{APPDATA}/notes"

MONGO = {
    # key: (image, env, extra args, discovered db_name)
    "mongo8auth": (
        "mongo:8.0",
        {
            "MONGO_INITDB_ROOT_USERNAME": "root",
            "MONGO_INITDB_ROOT_PASSWORD": MONGO_PASSWORD,
            "MONGO_INITDB_DATABASE": "app",
        },
        [],
        "app",
    ),
    "mongo8": ("mongo:8.0", {}, [], "admin"),
    "mongo7rs": ("mongo:7.0", {}, ["--replSet", "rs0"], "admin"),
}
# Backed up only (their images have no mongosh, so guided restore refuses
# them): the backup dumper must give mongodump its password on stdin where
# the Database Tools support --config (100.3.0+, mongo:4.4 and later) and
# fall back to argv on the legacy 4.2 tools.
MONGO_BACKUP_ONLY = {
    "mongo44auth": ("mongo:4.4", "root", 'p@ss w"rd $44', "100.3+"),
    "mongo42auth": ("mongo:4.2", "root", 'legacy w"rd $42', "legacy"),
}
DUMP_CHECK = r"""
import glob, json, sys
from app.services.db_dumper import dump_prefix
from app.services.dump_checks import read_mongo_archive
out = {}
for container, db_name in json.loads(sys.argv[1]):
    files = sorted(glob.glob(f"/config/dumps/{dump_prefix(container, 'mongodb', db_name)}_*.archive"))
    try:
        archive = read_mongo_archive(files[-1])
        out[container] = {f"{c.db}.{c.name}": c.documents for c in archive.collections if c.db != "admin"}
    except Exception as exc:
        out[container] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(out))
"""

REDIS = {
    "redis74aof": (
        "redis:7.4",
        ["redis-server", "--appendonly", "yes", "--requirepass", REDIS_PASSWORD],
    ),
    "redis80": ("redis:8.0", []),
    "redis62aof": ("redis:6.2", ["redis-server", "--appendonly", "yes"]),
}

# State of the databases a restore touches, plus what it must not touch.
MONGO_FINGERPRINT = r"""
const crypto = require("crypto");
const out = {databases: {}, users: []};
for (const d of db.adminCommand({listDatabases: 1, nameOnly: true}).databases) {
  if (["admin", "config", "local"].includes(d.name)) continue;
  const database = db.getSiblingDB(d.name);
  out.databases[d.name] = {};
  for (const info of database.getCollectionInfos()) {
    if (info.name.startsWith("system.")) continue;
    let docs = [];
    if (info.type === "collection") docs = database.getCollection(info.name).find().sort({_id: 1}).toArray();
    const indexes = info.type === "collection"
      ? database.getCollection(info.name).getIndexes().map((i) => i.name).sort() : [];
    out.databases[d.name][info.name] = [info.type, docs.length,
      crypto.createHash("md5").update(EJSON.stringify(docs)).digest("hex"), indexes];
  }
}
out.users = db.getSiblingDB("admin").system.users.find({}, {user: 1, db: 1}).toArray().map((u) => u._id).sort();
print("FP " + JSON.stringify(out));
"""


class GuidedHarness(Harness):
    # ---- environment (no host port) ----

    def up(self) -> None:
        self.claim_workdir()
        print(
            f"== starting Docker-in-Docker ({self.name('dind')}, state in {self.workdir})",
            flush=True,
        )
        for sub in ("docker", "run", "mnt-user", "backups"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        self.host("rm", "-f", self.name("dind"), check=False)
        repo = Path(__file__).resolve().parents[2]
        self.host(
            "run", "-d", "--name", self.name("dind"), "--privileged", "--memory", self.args.memory,
            "-e", "DOCKER_TLS_CERTDIR=",
            "-v", f"{self.workdir}/docker:/var/lib/docker",
            "-v", f"{self.workdir}/run:/var/run",
            "-v", f"{self.workdir}/mnt-user:/mnt/user",
            "-v", f"{self.workdir}/backups:{BACKUPS}",
            "-v", f"{repo}:/src:ro",
            self.args.dind_image, "dockerd", "--host=unix:///var/run/docker.sock",
        )  # fmt: skip
        self.wait_for(
            "the DinD daemon",
            lambda: self.dind("info", check=False).returncode == 0,
            60,
        )
        images = sorted(
            {spec[0] for spec in MONGO.values()}
            | {spec[0] for spec in REDIS.values()}
            | {spec[0] for spec in MONGO_BACKUP_ONLY.values()}
        )
        for image in images:
            self.dind("pull", "-q", image)
        print(f"== loading {self.args.image} into DinD", flush=True)
        save = subprocess.Popen(
            ["docker", "save", self.args.image], stdout=subprocess.PIPE
        )
        subprocess.run(
            ["docker", "-H", f"unix://{self.sock}", "load", "-q"],
            stdin=save.stdout,
            check=True,
        )
        if save.wait() != 0:
            raise RuntimeError(f"docker save {self.args.image} failed")
        self.start_fixtures()
        self.start_arkive()

    def start_fixtures(self) -> None:
        print("== starting MongoDB and Redis fixtures", flush=True)
        for key, (image, env, args, _db) in MONGO.items():
            env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
            self.dind("run", "-d", "--name", self.name(key), *env_args, image, *args)
        for key, (image, args) in REDIS.items():
            self.dind(
                "run",
                "-d",
                "--name",
                self.name(key),
                "-v",
                f"{APPDATA}/{key}:/data",
                image,
                *args,
            )
        for key, (image, user, password, _tools) in MONGO_BACKUP_ONLY.items():
            self.dind(
                "run", "-d", "--name", self.name(key), "-e", f"MONGO_INITDB_ROOT_USERNAME={user}",
                "-e", f"MONGO_INITDB_ROOT_PASSWORD={password}", image,
            )  # fmt: skip
        self.sh_dind(f"mkdir -p {NOTES_DIR} && echo hello > {NOTES_DIR}/readme.txt")
        for key in MONGO:
            self.wait_for(
                key,
                lambda k=key: self.mongo(k, "print('FP 1')", check=False) is not None,
                300,
            )
        self.mongo(
            "mongo7rs",
            "rs.initiate({_id: 'rs0', members: [{_id: 0, host: '127.0.0.1:27017'}]}); print('FP 1')",
        )
        self.wait_for(
            "the replica set primary",
            lambda: (
                self.mongo(
                    "mongo7rs",
                    "print('FP ' + JSON.stringify(db.hello().isWritablePrimary))",
                )
                is True
            ),
            60,
        )
        for key in REDIS:
            self.wait_for(
                key, lambda k=key: self.redis(k, "PING", check=False) == "PONG", 60
            )
        for key in MONGO_BACKUP_ONLY:
            self.wait_for(key, lambda k=key: self.legacy_mongo(k, "print('FP 1')", check=False) == 1, 300)

    def legacy_mongo(self, key: str, script: str, check: bool = True):
        """The legacy ``mongo`` shell of the 4.x images (test data only; the password is in argv here)."""
        _image, user, password, _tools = MONGO_BACKUP_ONLY[key]
        result = self.dind(
            "exec", self.name(key), "mongo", "--quiet", "--norc", "-u", user, "-p", password,
            "--authenticationDatabase", "admin", "--eval", script, check=False,
        )  # fmt: skip
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("FP "):
                return json.loads(line[3:])
        if check:
            raise RuntimeError(f"mongo in {key} failed: {result.stderr.strip()[-500:] or result.stdout[-500:]}")
        return None

    def watch_mongodump_argv(self) -> subprocess.Popen:
        """Record the command line of every mongodump process while a backup runs."""
        return subprocess.Popen(
            [
                "docker", "exec", self.name("dind"), "sh", "-c",
                "rm -f /tmp/arkive-stop-watch; while [ ! -e /tmp/arkive-stop-watch ]; do "
                "ps -o args | grep '^mongodump' || true; sleep 0.02; done",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )  # fmt: skip

    def check_backup_dumps(self, seen_argv: list[str]) -> None:
        """mongodump argv during the backup, and the dumps it wrote, for every MongoDB fixture."""
        r = self.report
        lines = sorted(set(seen_argv))
        print(f"   mongodump command lines seen during the backup: {len(lines)}", flush=True)
        for line in lines:
            print(f"     {line}", flush=True)
        r.check(any("--config=/dev/stdin" in line for line in lines), "mongodump seen reading --config from stdin")
        for password in (MONGO_PASSWORD, MONGO_BACKUP_ONLY["mongo44auth"][2]):
            r.check(not any(password in line for line in lines), "no supported mongodump had its password in argv")
        legacy = MONGO_BACKUP_ONLY["mongo42auth"][2]
        if any(legacy in line for line in lines):
            self.report.note("mongo:4.2 (legacy tools) mongodump ran with its password in argv, as documented")
        pairs = [[self.name(k), spec[3]] for k, spec in MONGO.items()]
        pairs += [[self.name(k), "admin"] for k in MONGO_BACKUP_ONLY]
        result = self.dind("exec", self.name("arkive"), "python", "-c", DUMP_CHECK, json.dumps(pairs), check=False)
        try:
            found = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            found = {}
        for key in [*MONGO, *MONGO_BACKUP_ONLY]:
            counts = found.get(self.name(key), {"error": result.stderr[-300:]})
            r.check("error" not in counts, f"{key}: backup dump is a complete archive", str(counts)[:300])
            if key in MONGO_BACKUP_ONLY:
                r.check(counts.get("app.notes") == 120, f"{key}: backup dump holds all documents", str(counts)[:300])

    def sh_dind(self, script: str, check: bool = True):
        """Run a POSIX sh script on the DinD "server" (it has a docker CLI and the bind-mounted shares)."""
        return self.host("exec", self.name("dind"), "sh", "-ec", script, check=check)

    # ---- engines ----

    def mongo(self, key: str, script: str, check: bool = True):
        env = MONGO[key][1]
        if env.get("MONGO_INITDB_ROOT_USERNAME"):
            auth = (
                f"db.getSiblingDB('admin').auth({json.dumps(env['MONGO_INITDB_ROOT_USERNAME'])}, "
                f"{json.dumps(env['MONGO_INITDB_ROOT_PASSWORD'])});\n"
            )
            script = auth + script
        result = self.dind(
            "exec", "-i", self.name(key), "mongosh", "--quiet", "--norc", "--file=/dev/stdin",
            input=script, check=False,
        )  # fmt: skip
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("FP "):
                return json.loads(line[3:])
        if check:
            raise RuntimeError(
                f"mongosh in {key} failed: {result.stderr.strip()[-500:] or result.stdout[-500:]}"
            )
        return None

    def redis(self, key: str, *args: str, check: bool = True) -> str | None:
        env = (
            ["-e", f"REDISCLI_AUTH={REDIS_PASSWORD}"]
            if "--requirepass" in REDIS[key][1]
            else []
        )
        result = self.dind(
            "exec", *env, self.name(key), "redis-cli", *args, check=False
        )
        if result.returncode != 0:
            if check:
                raise RuntimeError(f"redis-cli {args} in {key} failed: {result.stderr}")
            return None
        return result.stdout.strip()

    def mongo_fingerprint(self, key: str) -> dict:
        return self.mongo(key, MONGO_FINGERPRINT)

    def redis_fingerprint(self, key: str) -> dict:
        return {
            "db0 size": self.redis(key, "DBSIZE"),
            "db1 size": self.redis(key, "-n", "1", "DBSIZE"),
            "key:7": self.redis(key, "GET", "key:7"),
            "hash": self.redis(key, "HGETALL", "profile"),
            "list": self.redis(key, "LRANGE", "queue", "0", "-1"),
            "db1": self.redis(key, "-n", "1", "GET", "other"),
            "changed": self.redis(key, "GET", "changed"),
        }

    # ---- data ----

    def write_state_a(self) -> None:
        print("== writing state A", flush=True)
        for key in MONGO_BACKUP_ONLY:
            self.legacy_mongo(
                key,
                "db.getSiblingDB('app').notes.insertMany(Array.from({length: 120}, (_, i) => ({_id: i, n: i})));"
                " print('FP 1')",
            )
        self.mongo(
            "mongo8auth",
            """
            const app = db.getSiblingDB("app");
            app.notes.insertMany(Array.from({length: 300}, (_, i) => ({_id: i, text: "note " + i, n: i})));
            app.notes.createIndex({n: 1});
            app.tags.insertMany([{_id: 1, t: "red"}, {_id: 2, t: "blue"}]);
            app.createView("recent", "notes", [{$match: {n: {$gte: 290}}}]);
            db.getSiblingDB("other").keep.insertOne({_id: 1, v: "A"});
            print("FP 1");
            """,
        )
        for key in ("mongo8", "mongo7rs"):
            self.mongo(
                key,
                """
                const shop = db.getSiblingDB("shop");
                shop.orders.insertMany(Array.from({length: 500}, (_, i) => ({_id: i, total: i * 3, s: "x".repeat(50)})));
                shop.customers.insertMany(Array.from({length: 50}, (_, i) => ({_id: i, name: "c" + i})));
                shop.orders.createIndex({total: -1});
                db.getSiblingDB("logs").createCollection("metrics", {timeseries: {timeField: "t"}});
                db.getSiblingDB("logs").metrics.insertMany([{t: new Date(1790000000000), v: 1}, {t: new Date(1790000060000), v: 2}]);
                db.getSiblingDB("logs").plain.insertOne({_id: 1});
                print("FP 1");
                """,
            )
        for key in REDIS:
            for i in range(200):
                self.redis(key, "SET", f"key:{i}", f"value-{i}")
            self.redis(key, "HSET", "profile", "name", "alice", "role", "admin")
            self.redis(key, "RPUSH", "queue", "a", "b", "c")
            self.redis(key, "-n", "1", "SET", "other", "db1-value")

    def write_state_b(self) -> None:
        print("== changing the data (state B)", flush=True)
        self.mongo(
            "mongo8auth",
            """
            const app = db.getSiblingDB("app");
            app.notes.deleteMany({n: {$lt: 100}});
            app.notes.updateMany({}, {$set: {edited: true}});
            app.tags.drop();
            app.added_later.insertOne({_id: 1});
            db.getSiblingDB("other").keep.insertOne({_id: 2, v: "B"});
            print("FP 1");
            """,
        )
        for key in ("mongo8", "mongo7rs"):
            self.mongo(
                key,
                """
                db.getSiblingDB("shop").orders.deleteMany({_id: {$gte: 250}});
                db.getSiblingDB("logs").dropDatabase();
                db.getSiblingDB("created_later").x.insertOne({_id: 1});
                print("FP 1");
                """,
            )
        for key in REDIS:
            self.redis(key, "FLUSHALL")
            self.redis(key, "SET", "changed", "yes")

    # ---- guided restore ----

    def wait_operation(self, op_id: str, timeout: float = 900) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = self.api("GET", f"/api/restore/database/{op_id}")
            if op.get("status") != "running":
                return op
            time.sleep(2)
        raise RuntimeError(f"guided restore {op_id} did not finish")

    def selection(
        self,
        target_id: str,
        snapshot_id: str,
        container: str,
        db_type: str,
        db_name: str,
    ) -> dict:
        return {
            "container_name": container,
            "db_type": db_type,
            "db_name": db_name,
            "snapshot_id": snapshot_id,
            "target_id": target_id,
        }

    def report_op(self, label: str, op: dict) -> None:
        print(f"   {label}: {op['status']} {op.get('error_message') or ''}", flush=True)
        for step in op["steps"]:
            print(
                f"     - {step['key']}: {step['status']} {step.get('detail') or ''}",
                flush=True,
            )
        for warning in op.get("warnings") or []:
            print(f"     ! {warning}", flush=True)

    def check_mongo(
        self, key: str, target_id: str, snapshot_id: str, state_a: dict
    ) -> None:
        r = self.report
        db_name = MONGO[key][3]
        container = self.name(key)
        state_b = self.mongo_fingerprint(key)
        in_scope = (lambda d: d == db_name) if db_name != "admin" else (lambda d: True)

        def scoped(fp: dict) -> dict:
            return {d: v for d, v in fp["databases"].items() if in_scope(d)}

        r.check(
            scoped(state_a) != scoped(state_b), f"{key}: state B differs from state A"
        )
        sel = self.selection(target_id, snapshot_id, container, "mongodb", db_name)
        preview = self.api("POST", "/api/restore/database/preview", sel)
        r.check(
            preview["method"] == "load" and not preview["blockers"],
            f"{key}: preview",
            json.dumps(preview)[:400],
        )
        started = self.api(
            "POST",
            "/api/restore/database",
            {**sel, "confirm": db_name, "stop_containers": []},
        )
        op = self.wait_operation(started["id"])
        self.report_op(f"{key} restore", op)
        r.check(
            op["status"] == "completed",
            f"{key}: guided restore completed",
            str(op.get("error_message")),
        )
        restored = self.mongo_fingerprint(key)
        r.check(
            scoped(restored) == scoped(state_a),
            f"{key}: state A is back",
            self.diff(scoped(state_a), scoped(restored)),
        )
        if db_name != "admin":
            others = {d: v for d, v in restored["databases"].items() if d != db_name}
            others_b = {d: v for d, v in state_b["databases"].items() if d != db_name}
            r.check(
                others == others_b,
                f"{key}: other databases untouched",
                self.diff(others_b, others),
            )
        r.check(
            restored["users"] == state_b["users"],
            f"{key}: users in admin untouched",
            str(restored["users"]),
        )
        leftovers = self.dind(
            "exec",
            container,
            "sh",
            "-c",
            "ls -d /tmp/arkive-restore.* 2>/dev/null || true",
        )
        r.check(
            not leftovers.stdout.strip(),
            f"{key}: no password file left in the container",
            leftovers.stdout,
        )
        load = next(
            (s.get("detail") or "" for s in op["steps"] if s["key"] == "load"), ""
        )
        if MONGO[key][1].get("MONGO_INITDB_ROOT_PASSWORD"):
            r.check(
                "private file" in load,
                f"{key}: password read from a private file, not argv",
                load,
            )

        undo_started = self.api(
            "POST",
            f"/api/restore/database/{op['id']}/undo",
            {"confirm": db_name, "stop_containers": []},
        )
        undo = self.wait_operation(undo_started["id"])
        self.report_op(f"{key} undo", undo)
        r.check(
            undo["status"] == "completed",
            f"{key}: undo completed",
            str(undo.get("error_message")),
        )
        after_undo = self.mongo_fingerprint(key)
        r.check(
            scoped(after_undo) == scoped(state_b), f"{key}: state B is back after undo",
            self.diff(scoped(state_b), scoped(after_undo)),
        )  # fmt: skip

    @staticmethod
    def diff(expected: dict, actual: dict) -> str:
        keys = sorted(set(expected) | set(actual))
        return "; ".join(
            f"{k}: {expected.get(k)!r} != {actual.get(k)!r}"
            for k in keys
            if expected.get(k) != actual.get(k)
        )[:600]

    def run_steps(
        self, key: str, instructions: list[dict], replace: tuple[str, str] | None = None
    ):
        commands = []
        for step in instructions[:-1]:  # the last one is the safety copy's path
            command = step.get("command")
            if not command or " -it " in command:
                continue  # interactive check (password prompt); the harness checks the data itself
            if replace:
                command = command.replace(replace[0], replace[1])
            commands.append(command)
        print(f"   running {len(commands)} steps for {key}:", flush=True)
        for command in commands:
            print(f"     $ {command}", flush=True)
        return self.sh_dind("\n".join(commands), check=False)

    def check_redis(
        self, key: str, target_id: str, snapshot_id: str, state_a: dict
    ) -> None:
        r = self.report
        container = self.name(key)
        state_b = self.redis_fingerprint(key)
        r.check(state_a != state_b, f"{key}: state B differs from state A")
        sel = self.selection(target_id, snapshot_id, container, "redis", "redis")
        preview = self.api("POST", "/api/restore/database/preview", sel)
        r.check(
            preview["method"] == "manual" and not preview["blockers"],
            f"{key}: preview",
            json.dumps(preview)[:400],
        )
        started = self.api(
            "POST",
            "/api/restore/database",
            {**sel, "confirm": "", "stop_containers": []},
        )
        op = self.wait_operation(started["id"])
        self.report_op(f"{key} restore", op)
        r.check(
            op["status"] == "action_required",
            f"{key}: file prepared",
            str(op.get("error_message")),
        )
        if op["status"] != "action_required":
            return
        instructions = op["instructions"]
        copy = next(
            s["command"]
            for s in instructions
            if s["text"].startswith("Copy the restored data file")
        )
        restored_path = shlex.split(copy)[1]
        safety_path = instructions[-1]["command"]
        result = self.run_steps(key, instructions)
        r.check(
            result.returncode == 0,
            f"{key}: the swap-in steps run",
            result.stderr[-500:],
        )
        self.wait_for(key, lambda: self.redis(key, "PING", check=False) == "PONG", 60)
        r.check(
            self.redis_fingerprint(key) == state_a,
            f"{key}: state A is back",
            str(self.redis_fingerprint(key)),
        )
        self.dind("restart", container)
        self.wait_for(key, lambda: self.redis(key, "PING", check=False) == "PONG", 60)
        r.check(
            self.redis_fingerprint(key) == state_a, f"{key}: state A survives a restart (persisted as Redis loads it)",
            str(self.redis_fingerprint(key)),
        )  # fmt: skip
        if REDIS[key][1] and "--appendonly" in REDIS[key][1]:
            r.check(
                self.redis(key, "CONFIG", "GET", "appendonly").endswith("yes"),
                f"{key}: append-only file still on",
            )

        result = self.run_steps(key, instructions, replace=(restored_path, safety_path))
        r.check(
            result.returncode == 0,
            f"{key}: the same steps with the safety copy run",
            result.stderr[-500:],
        )
        self.wait_for(key, lambda: self.redis(key, "PING", check=False) == "PONG", 60)
        self.dind("restart", container)
        self.wait_for(key, lambda: self.redis(key, "PING", check=False) == "PONG", 60)
        r.check(
            self.redis_fingerprint(key) == state_b, f"{key}: state B is back after going back",
            str(self.redis_fingerprint(key)),
        )  # fmt: skip

    def roundtrip(self) -> int:
        r = self.report
        if not self.api_key:
            result = self.api(
                "POST",
                "/api/auth/setup",
                {
                    "encryption_password": ENCRYPTION_PASSWORD,
                    "storage": {
                        "type": "local",
                        "name": "Local",
                        "path": f"{BACKUPS}/repo",
                    },
                    "directories": [NOTES_DIR],
                },
                auth=False,
            )
            self.api_key = result["api_key"]
            (self.workdir / "api_key").write_text(self.api_key)
        targets = self.api("GET", "/api/targets")
        targets = (
            targets.get("items", targets.get("targets", targets))
            if isinstance(targets, dict)
            else targets
        )
        jobs = self.api("GET", "/api/jobs")
        jobs = (
            jobs.get("items", jobs.get("jobs", jobs))
            if isinstance(jobs, dict)
            else jobs
        )
        target_id = targets[0]["id"]

        self.write_state_a()
        mongo_a = {key: self.mongo_fingerprint(key) for key in MONGO}
        redis_a = {key: self.redis_fingerprint(key) for key in REDIS}
        watcher = self.watch_mongodump_argv()
        try:
            run = self.run_backup(jobs)
        finally:
            self.sh_dind("touch /tmp/arkive-stop-watch", check=False)
            seen_argv = (watcher.communicate(timeout=60)[0] or "").splitlines()
        print(
            f"== backup run {run['id']}: {run['status']} {run.get('error_message') or ''}",
            flush=True,
        )
        r.check(
            run["status"] == "success",
            "backup run status is success",
            str(run.get("error_message")),
        )
        dumped = {
            (d["container_name"], d["db_name"]): d for d in run.get("databases", [])
        }
        for key, spec in MONGO.items():
            entry = dumped.get((self.name(key), spec[3]))
            r.check(
                bool(entry) and entry["status"] == "success",
                f"dumped {key}",
                str(entry),
            )
        for key in REDIS:
            entry = dumped.get((self.name(key), "redis"))
            r.check(
                bool(entry) and entry["status"] == "success",
                f"dumped {key}",
                str(entry),
            )
        for key in MONGO_BACKUP_ONLY:
            entry = dumped.get((self.name(key), "admin"))
            r.check(bool(entry) and entry["status"] == "success", f"dumped {key}", str(entry))
        self.check_backup_dumps(seen_argv)
        snapshot_id = next(
            (t["snapshot_id"] for t in run.get("targets", []) if t.get("snapshot_id")),
            None,
        )
        if not snapshot_id:
            raise RuntimeError(f"backup run recorded no snapshot: {run.get('targets')}")
        options = {
            (o["container_name"], o["db_type"]): o
            for o in self.api("GET", "/api/restore/database/options")["databases"]
        }
        for key, spec in MONGO.items():
            option = options.get((self.name(key), "mongodb"), {})
            r.check(
                option.get("method") == "load",
                f"{key}: offered as an in-place restore",
                str(option),
            )
        for key in REDIS:
            option = options.get((self.name(key), "redis"), {})
            r.check(
                option.get("method") == "manual",
                f"{key}: offered as a prepared restore",
                str(option),
            )

        self.write_state_b()
        print("== guided restores", flush=True)
        for key in MONGO:
            self.check_mongo(key, target_id, snapshot_id, mongo_a[key])
        for key in REDIS:
            self.check_redis(key, target_id, snapshot_id, redis_a[key])
        print(f"\n== {len(r.passed)} passed, {len(r.failed)} failed", flush=True)
        for line in r.failed:
            print(f"   FAIL {line}")
        return 1 if r.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--image",
        default="arkive-rt:dev",
        help="Arkive image to test (must exist on the host)",
    )
    parser.add_argument(
        "--prefix", default="arkive-grt", help="name prefix of everything created"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8320,
        help="API port inside the Arkive container (not published)",
    )
    parser.add_argument(
        "--workdir",
        help="state directory, new or empty; removed on teardown (default /tmp/<prefix>)",
    )
    parser.add_argument(
        "--memory", default="8g", help="memory limit of the DinD container"
    )
    parser.add_argument(
        "--dind-image", default=DIND_IMAGE, help="Docker-in-Docker image"
    )
    parser.add_argument(
        "--live-code",
        action="store_true",
        help="mount this checkout's backend/ into the image",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the environment running"
    )
    parser.add_argument(
        "--down", action="store_true", help="only remove an environment left by --keep"
    )
    args = parser.parse_args()
    if not shutil.which("docker"):
        print("docker CLI not found", file=sys.stderr)
        return 2
    harness = GuidedHarness(args)
    if args.down:
        harness.down()
        return 0
    try:
        harness.up()
        return harness.roundtrip()
    except Exception as exc:
        print(f"guided round trip aborted: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(shlex.join(exc.cmd), exc.stderr, file=sys.stderr)
        return 2
    finally:
        if not args.keep:
            harness.down()


if __name__ == "__main__":
    sys.exit(main())
