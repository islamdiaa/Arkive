#!/usr/bin/env python3
"""Upgrade-path test: an install made by an old Arkive release must keep
working after its container is re-created from an image built from this tree.

The test:

1. starts the old release (default ``islamdiaa/arkive:0.2.4``), completes
   setup through its API with a local storage target, and runs a backup of a
   small sample directory plus a throwaway Postgres container;
2. stops it and starts the new image on the same ``/config`` and backup
   volume, then checks that
   - it starts (the keyfile guard accepts the old ``.keyfile``, unchanged),
   - every schema migration is applied, data migrations included,
   - the old API key still works and the old run history is still there,
   - the old snapshot is listed, both from the cache and from the repository,
   - a file and the database dump can be restored from the old snapshot (the
     dump is loaded into a fresh Postgres and its rows are checked), and a
     restore drill passes on it,
   - a new backup succeeds, dumps the database, keeps the old snapshot, and
     its dump restores with the rows written after the upgrade,
   - the other jobs the old setup created succeed,
   - without its ``.keyfile`` the upgraded database is refused.

See docs/upgrade-path-test.md.

Isolation. Arkive needs the Docker socket and 0.2.4 dumps every database it
can see, so by default everything runs inside a private Docker daemon
(``docker:dind``, privileged) that only holds the test containers. Use
``--docker host`` only on a throwaway machine such as a CI runner; it refuses
to run when the daemon has containers the test didn't create.

Every container and volume the test creates is named with ``--name-prefix``
and removed at the end (``--keep`` leaves them for debugging). Test data lives
in those named volumes, never in host directories.

Needs: docker, python3 (stdlib only). Run ``make test-upgrade`` or
``scripts/upgrade-path-test.py --help``.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OLD_IMAGE = "islamdiaa/arkive:0.2.4"
DEFAULT_POSTGRES_IMAGE = "postgres:16-alpine"
DEFAULT_DIND_IMAGE = "docker:29.5.3-dind"
DEFAULT_PREFIX = "arkive-upgrade-test-"
DEFAULT_PORT = 8310

# Paths inside the Arkive container.
SAMPLE_MOUNT = "/srv/sample"
BACKUP_MOUNT = "/backups"

PG_USER = "postgres"
PG_PASSWORD = "upgrade-test-password"  # nosec B105 - throwaway test database
PG_DB = "appdb"

TERMINAL_RUN_STATES = {"success", "partial", "failed", "cancelled", "skipped"}

# Notification events the 0.2.4 UI offered (some no longer exist), and what
# the later migrations add to channels subscribed to backup.failed.
OLD_EVENTS = ["backup.failed", "system.error"]
MIGRATED_EVENTS = {"backup.partial", "backup.stale", "drill.failed", "verification.failed", "target.error"}


class UpgradeCheckError(Exception):
    """A check failed; the message says which and why."""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise UpgradeCheckError(message)
    log(f"  ok: {message}")


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


class Docker:
    """Runs docker CLI commands against the host daemon or the private one."""

    def __init__(self, base: list[str]):
        self.base = base

    def run(
        self,
        *args: str,
        check: bool = True,
        input: bytes | None = None,
        timeout: float = 300,
    ) -> subprocess.CompletedProcess:
        argv = [*self.base, *args]
        try:
            proc = subprocess.run(argv, input=input, capture_output=True, timeout=timeout)  # nosec B603
        except subprocess.TimeoutExpired as e:
            if check:
                raise UpgradeCheckError(f"docker {' '.join(args[:3])} ... timed out after {timeout}s") from e
            return subprocess.CompletedProcess(argv, 124, e.stdout or b"", e.stderr or b"")
        if check and proc.returncode != 0:
            raise UpgradeCheckError(
                f"docker {' '.join(args[:3])} ... failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()[-2000:]}"
            )
        return proc

    def out(self, *args: str, **kwargs: Any) -> str:
        return self.run(*args, **kwargs).stdout.decode(errors="replace").strip()

    def exists(self, kind: str, name: str) -> bool:
        return self.run(kind, "inspect", name, check=False, timeout=60).returncode == 0


HOST = Docker(["docker"])


def foreign_containers(names: list[str], prefix: str) -> list[str]:
    """Containers on a daemon that this test did not create."""
    return [n for n in names if n and not n.startswith(prefix)]


def remove_all(docker: Docker, objects: list[tuple[str, str]], attempts: int = 6) -> list[str]:
    """Remove containers/volumes, retrying (a busy daemon can be slow to kill a
    container, and a volume can't go while its container exists). Returns what
    is left."""
    remaining = list(objects)
    for attempt in range(attempts):
        for kind, name in list(remaining):
            if not docker.exists(kind, name):
                remaining.remove((kind, name))
                continue
            args = ("rm", "-fv", name) if kind == "container" else ("volume", "rm", "-f", name)
            if docker.run(*args, check=False, timeout=300).returncode == 0:
                remaining.remove((kind, name))
        if not remaining:
            break
        if attempt < attempts - 1:
            time.sleep(5)
    return [f"{kind} {name}" for kind, name in remaining]


def image_present(docker: Docker, image: str) -> bool:
    return docker.run("image", "inspect", image, check=False, timeout=60).returncode == 0


def ensure_host_image(image: str) -> None:
    if not image_present(HOST, image):
        log(f"pulling {image}")
        HOST.run("pull", image, timeout=900)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = ""

    def call(self, method: str, path: str, body: Any = None, timeout: float = 120) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - local test URL
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return status, payload

    def ok(self, method: str, path: str, body: Any = None, expect: tuple[int, ...] = (200,), **kw: Any) -> Any:
        status, payload = self.call(method, path, body, **kw)
        if status not in expect:
            raise UpgradeCheckError(f"{method} {path} returned HTTP {status}: {str(payload)[:2000]}")
        return payload


def items(payload: Any, *keys: str) -> list:
    """List from a response that is either a list or a dict wrapping one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (*keys, "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise UpgradeCheckError(f"unexpected response shape: {str(payload)[:500]}")


# ---------------------------------------------------------------------------
# Test environment
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.prefix = args.name_prefix
        self.port = args.port
        self.mode = args.docker
        self.dind = f"{self.prefix}dind"
        self.dind_volume = f"{self.prefix}dind-lib"
        self.pg = f"{self.prefix}postgres"
        self.pg_verify = f"{self.prefix}postgres-verify"
        self.old = f"{self.prefix}arkive-old"
        self.new = f"{self.prefix}arkive-new"
        self.probe = f"{self.prefix}arkive-probe"
        self.helper = f"{self.prefix}helper"
        # Named volumes rather than host directories: they work the same
        # whether the docker CLI runs on the host, in a container, or talks
        # to the private daemon.
        self.volumes = {
            "config": f"{self.prefix}config",
            "cache": f"{self.prefix}cache",
            "backups": f"{self.prefix}backups",
            "sample": f"{self.prefix}sample",
        }
        self.new_image = args.new_image or f"{self.prefix}new:local"
        self.built_new_image = False
        # Set once the name checks pass: cleanup never removes objects that
        # existed before this run.
        self.owns_names = False
        self.docker = HOST if self.mode == "host" else Docker(["docker", "exec", "-i", self.dind, "docker"])
        self.api = Api(f"http://127.0.0.1:{self.port}")
        self.containers = [self.old, self.new, self.probe, self.pg, self.pg_verify, self.helper]

    # -- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        if self.mode == "host":
            self._refuse_shared_daemon()
            existing = [("container", n) for n in self.containers] + [("volume", v) for v in self.volumes.values()]
        else:
            existing = [("container", self.dind), ("volume", self.dind_volume)]
        for kind, name in existing:
            if HOST.exists(kind, name):
                raise UpgradeCheckError(f"{kind} {name} already exists; remove it or pick another --name-prefix")
        self.owns_names = True

        if not self.args.new_image:
            log(f"building {self.new_image} from {REPO_ROOT}")
            HOST.run("build", "-t", self.new_image, str(REPO_ROOT), timeout=3600)
            self.built_new_image = True
        elif not image_present(HOST, self.new_image):
            raise UpgradeCheckError(f"--new-image {self.new_image} is not present on the host daemon")
        for image in (self.args.old_image, self.args.postgres_image):
            ensure_host_image(image)

        if self.mode == "dind":
            self._start_private_daemon()
            for image in (self.args.old_image, self.new_image, self.args.postgres_image):
                self._load_image(image)
        for volume in self.volumes.values():
            self.docker.run("volume", "create", volume)

    def _refuse_shared_daemon(self) -> None:
        names = HOST.out("ps", "-a", "--format", "{{.Names}}").split()
        foreign = foreign_containers(names, self.prefix)
        if foreign:
            raise UpgradeCheckError(
                "--docker host needs a daemon with no other containers: Arkive discovers (and the old "
                f"release dumps) every database it can see. Found: {', '.join(foreign[:10])}. "
                "Use --docker dind (the default) instead."
            )

    def _start_private_daemon(self) -> None:
        ensure_host_image(self.args.dind_image)
        log(f"starting private Docker daemon {self.dind}")
        HOST.run("volume", "create", self.dind_volume)
        HOST.run(
            "run", "-d", "--privileged",
            "--name", self.dind,
            # The daemon's name is what new Arkive versions record as the
            # restic host; use a generic fixture value.
            "--hostname", "tower",
            "-e", "DOCKER_TLS_CERTDIR=",
            "-v", f"{self.dind_volume}:/var/lib/docker",
            "-p", f"127.0.0.1:{self.port}:8200",
            self.args.dind_image,
            # Unix socket only: no TCP listener on the host network.
            "dockerd", "--host=unix:///var/run/docker.sock",
        )  # fmt: skip
        # A busy host can take a couple of minutes to boot a fresh daemon.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self.docker.run("info", check=False, timeout=30).returncode == 0:
                return
            time.sleep(2)
        raise UpgradeCheckError(
            "private Docker daemon did not come up:\n" + HOST.out("logs", "--tail", "50", self.dind)
        )

    def _load_image(self, image: str) -> None:
        log(f"loading {image} into the private daemon")
        save = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)  # nosec B603 B607
        try:
            load = subprocess.run(  # nosec B603 B607
                ["docker", "exec", "-i", self.dind, "docker", "load", "-q"],
                stdin=save.stdout,
                capture_output=True,
                timeout=1800,
            )
            save.wait(timeout=120)
        except subprocess.TimeoutExpired as e:
            raise UpgradeCheckError(f"loading {image} into the private daemon timed out") from e
        finally:
            if save.stdout:
                save.stdout.close()
            if save.poll() is None:
                save.kill()
                save.wait()
        if save.returncode != 0 or load.returncode != 0:
            raise UpgradeCheckError(f"loading {image} failed: {load.stderr.decode(errors='replace')[-1000:]}")

    def cleanup(self) -> None:
        if not self.owns_names:
            return
        if self.args.keep:
            log(f"--keep: leaving the {self.prefix}* containers and volumes in place")
            return
        log("cleaning up")
        if self.mode == "dind":
            # Everything else lives inside the private daemon's storage. A
            # graceful stop lets it shut its own containers down first.
            HOST.run("stop", "-t", "60", self.dind, check=False, timeout=180)
            leftovers = remove_all(HOST, [("container", self.dind), ("volume", self.dind_volume)])
        else:
            leftovers = remove_all(
                HOST, [("container", n) for n in self.containers] + [("volume", v) for v in self.volumes.values()]
            )
        if self.built_new_image:
            HOST.run("image", "rm", self.new_image, check=False, timeout=120)
        if leftovers:
            log(f"WARNING: could not remove {', '.join(leftovers)}; remove them by hand")

    def print_logs(self, lines: int = 80) -> None:
        for name in (self.old, self.new):
            proc = self.docker.run("logs", "--tail", str(lines), name, check=False, timeout=60)
            if proc.returncode == 0:
                print(f"----- last {lines} log lines of {name} -----", flush=True)
                sys.stdout.write((proc.stdout + proc.stderr).decode(errors="replace"))
                sys.stdout.flush()

    # -- containers --------------------------------------------------------

    def start_arkive(self, name: str, image: str, serve: bool = True) -> None:
        """Run Arkive on the shared volumes; ``serve`` publishes the API and waits for it."""
        publish = ["-p", "8200:8200" if self.mode == "dind" else f"127.0.0.1:{self.port}:8200"] if serve else []
        log(f"starting {image} as {name}")
        self.docker.run(
            "run", "-d", "--name", name,
            # As the Unraid template runs it (Docker socket access, flash backup).
            "--user", "0:0",
            "-e", "TZ=UTC",
            "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
            "-v", f"{self.volumes['config']}:/config",
            "-v", f"{self.volumes['cache']}:/cache",
            "-v", f"{self.volumes['backups']}:{BACKUP_MOUNT}",
            "-v", f"{self.volumes['sample']}:{SAMPLE_MOUNT}:ro",
            *publish,
            image,
        )  # fmt: skip
        if serve:
            self.wait_ready(name)

    def wait_exit(self, name: str, timeout: float = 180) -> str:
        """Wait for a container to stop; return its logs."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.docker.out("inspect", "-f", "{{.State.Status}}", name) in ("exited", "dead"):
                proc = self.docker.run("logs", name, check=False)
                return (proc.stdout + proc.stderr).decode(errors="replace")
            time.sleep(1)
        raise UpgradeCheckError(f"{name} was still running after {timeout}s")

    def in_config(self, script: str) -> None:
        """Run a shell snippet against the config volume in a throwaway container."""
        self.docker.run(
            "run", "--rm", "--name", self.helper, "-v", f"{self.volumes['config']}:/config",
            self.args.postgres_image, "sh", "-ec", script,
        )  # fmt: skip

    def wait_ready(self, name: str, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            state = self.docker.out("inspect", "-f", "{{.State.Status}}", name)
            if state in ("exited", "dead"):
                proc = self.docker.run("logs", "--tail", "60", name, check=False)
                logs = (proc.stdout + proc.stderr).decode(errors="replace")
                raise UpgradeCheckError(f"{name} exited during startup:\n{logs}")
            try:
                status, payload = self.api.call("GET", "/api/status", timeout=10)
                if status == 200 and isinstance(payload, dict):
                    return payload
                last = f"HTTP {status}"
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
            time.sleep(1)
        raise UpgradeCheckError(f"{name} did not answer /api/status within {timeout}s ({last})")

    def stop(self, name: str) -> None:
        self.docker.run("stop", "-t", "30", name, timeout=120)

    def exec(self, name: str, *cmd: str, input: bytes | None = None, timeout: float = 300) -> bytes:
        flags = ["-i"] if input is not None else []
        return self.docker.run("exec", *flags, name, *cmd, input=input, timeout=timeout).stdout

    def start_postgres(self, name: str) -> None:
        self.docker.run(
            "run", "-d", "--name", name,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            self.args.postgres_image,
        )  # fmt: skip
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            # pg_isready answers during the init-script restart too; a query
            # over TCP only works once the final server is up.
            probe = self.docker.run(
                "exec", name, "psql", "-h", "127.0.0.1", "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1",
                check=False, timeout=30,
            )  # fmt: skip
            if probe.returncode == 0 and probe.stdout.strip() == b"1":
                return
            time.sleep(1)
        raise UpgradeCheckError(f"{name} did not become ready")

    def psql(self, name: str, sql: str, db: str = PG_DB) -> str:
        return (
            self.exec(name, "psql", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", PG_USER, "-d", db, "-tAc", sql)
            .decode()
            .strip()
        )

    def config_sha256(self, container: str, path: str) -> str:
        return self.exec(container, "sha256sum", path).decode().split()[0]

    def schema_versions(self, container: str) -> dict:
        script = (
            "import json, sqlite3\n"
            "from app.core.database import MIGRATIONS\n"
            "conn = sqlite3.connect('file:/config/arkive.db?mode=ro', uri=True)\n"
            "applied = sorted(r[0] for r in conn.execute('SELECT version FROM schema_version'))\n"
            "print(json.dumps({'applied': applied, 'code': max(MIGRATIONS)}))\n"
        )
        return json.loads(self.exec(container, "python", "-c", script))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def sample_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for directory in sorted({str(Path(rel).parent) for rel in files} - {"."}):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for rel, content in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def write_sample_data(env: Env, marker: str) -> dict[str, bytes]:
    files = {
        "notes.txt": f"upgrade-path sample {marker}\n".encode(),
        "nested/settings.json": json.dumps({"marker": marker, "version": 1}).encode(),
        "nested/blob.bin": secrets.token_bytes(64 * 1024),
    }
    env.docker.run(
        "run", "--rm", "-i", "--name", env.helper,
        "-v", f"{env.volumes['sample']}:/sample",
        env.args.postgres_image, "tar", "-x", "-C", "/sample",
        input=sample_tar(files),
    )  # fmt: skip
    return files


def wait_for_run(api: Api, run_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    run: dict = {}
    while time.monotonic() < deadline:
        run = api.ok("GET", f"/api/jobs/runs/{run_id}")
        if run.get("status") in TERMINAL_RUN_STATES:
            return run
        time.sleep(2)
    raise UpgradeCheckError(f"run {run_id} did not finish within {timeout}s (last: {run})")


def run_full_job(env: Env, job_id: str) -> dict:
    trigger = env.api.ok("POST", f"/api/jobs/{job_id}/run", expect=(200, 202))
    run_id = trigger["run_id"]
    log(f"  run {run_id} started")
    run = wait_for_run(env.api, run_id, env.args.run_timeout)
    log(
        f"  run {run_id}: status={run.get('status')} databases_dumped={run.get('databases_dumped')} "
        f"error={run.get('error_message')!r}"
    )
    return run


def snapshot_ids(env: Env) -> set[str]:
    """Short and full IDs of the snapshots the API lists."""
    payload = env.api.ok("GET", "/api/snapshots")
    ids: set[str] = set()
    for snap in items(payload, "snapshots"):
        ids.update(i for i in (snap.get("id"), snap.get("full_id")) if i)
    return ids


def same_snapshot(a: str, b: str) -> bool:
    """Runs record full snapshot IDs, listings and drills short ones."""
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def restore(env: Env, snapshot_id: str, target_id: str, paths: list[str]) -> Path:
    result = env.api.ok(
        "POST",
        "/api/restore",
        {"snapshot_id": snapshot_id, "target": target_id, "paths": paths},
        timeout=600,
    )
    check(
        result.get("status") == "success",
        f"restore of {paths} from snapshot {snapshot_id} succeeded ({result.get('message', '')[:300]!r})",
    )
    restore_to = result["restore_to"]
    if not restore_to.startswith("/config/"):
        raise UpgradeCheckError(f"restore went to {restore_to}, expected a directory under /config")
    return Path(restore_to)


def dump_path_of(run: dict, container: str) -> str:
    """The dump file a run wrote for container/PG_DB (``job_run_databases.host_path``)."""
    for d in run.get("databases", []):
        if d.get("container_name") == container and d.get("db_name") == PG_DB and d.get("status") == "success":
            path = d.get("host_path") or ""
            if not path.startswith("/config/dumps/"):
                raise UpgradeCheckError(f"run {run.get('id')} recorded an unexpected dump path: {path!r}")
            return path
    raise UpgradeCheckError(f"run {run.get('id')} has no successful dump of {container}/{PG_DB}")


def restore_dump(env: Env, snapshot_id: str, target_id: str, dump_path: str) -> str:
    """Restore one dump file from a snapshot; return its restored path in the Arkive container."""
    restored = restore(env, snapshot_id, target_id, [dump_path])
    return str(restored / dump_path.lstrip("/"))


def run_restore_drill(env: Env, timeout: float = 600) -> dict:
    started = env.api.ok("POST", "/api/drills/run", expect=(202,))
    drill_id = started["drill_id"]
    deadline = time.monotonic() + timeout
    drill: dict = {}
    while time.monotonic() < deadline:
        drill = env.api.ok("GET", f"/api/drills/{drill_id}")
        if drill.get("status") != "running":
            return drill
        time.sleep(2)
    raise UpgradeCheckError(f"restore drill {drill_id} did not finish within {timeout}s (last: {drill})")


def load_dump_and_read_markers(env: Env, arkive: str, dump_path: str) -> list[str]:
    """Load a restored dump into a fresh Postgres and return its marker rows."""
    raw = env.exec(arkive, "cat", dump_path)
    sql = gzip.decompress(raw) if dump_path.endswith(".gz") else raw
    if b"PostgreSQL database dump complete" not in sql:
        raise UpgradeCheckError(f"{dump_path} is not a complete pg_dump")
    env.start_postgres(env.pg_verify)
    try:
        # The dump was made with --create: load it from the maintenance DB.
        env.docker.run(
            "exec", "-i", env.pg_verify,
            "psql", "-v", "ON_ERROR_STOP=1", "-q", "-h", "127.0.0.1", "-U", PG_USER, "-d", "postgres",
            input=sql, timeout=300,
        )  # fmt: skip
        rows = env.psql(env.pg_verify, "SELECT note FROM upgrade_marker ORDER BY id")
    finally:
        env.docker.run("rm", "-fv", env.pg_verify, check=False, timeout=120)
    return [r for r in rows.splitlines() if r]


def phase_old(env: Env, marker: str) -> dict:
    args = env.args
    log(f"== phase 1: {args.old_image}")
    sample = write_sample_data(env, marker)

    log("starting sample Postgres")
    env.start_postgres(env.pg)
    env.psql(env.pg, "CREATE TABLE upgrade_marker (id serial PRIMARY KEY, note text NOT NULL)")
    env.psql(env.pg, f"INSERT INTO upgrade_marker (note) VALUES ('before-upgrade-{marker}')")  # nosec B608 - hex marker

    env.start_arkive(env.old, args.old_image)
    session = env.api.ok("GET", "/api/auth/session")
    check(session.get("setup_required") is True, "old release starts in setup mode")

    # Schedules nobody will hit during the test, so only manual runs happen.
    never = "0 3 1 1 *"
    setup = env.api.ok(
        "POST",
        "/api/auth/setup",
        {
            "encryption_password": f"upgrade-test-{marker}",
            "storage": {"type": "local", "name": "Upgrade test", "path": BACKUP_MOUNT},
            "directories": [SAMPLE_MOUNT],
            "schedules": {"db_dump": never, "cloud_sync": never, "flash": never},
            "run_first_backup": False,
        },
        expect=(200, 201),
    )
    env.api.api_key = setup["api_key"]
    log(f"  setup complete, {setup.get('jobs_created')} jobs created")

    targets = items(env.api.ok("GET", "/api/targets"), "targets")
    check(len(targets) == 1 and targets[0].get("type") == "local", "setup created the local target")
    target_id = targets[0]["id"]
    jobs = items(env.api.ok("GET", "/api/jobs"), "jobs")
    by_type = {j.get("type"): j["id"] for j in jobs}
    check(sorted(by_type) == ["db_dump", "flash", "full"], f"setup created the default jobs ({sorted(by_type)})")
    job_id = by_type["full"]

    # A channel with events the old UI offered; its URL is stored encrypted.
    channel = env.api.ok(
        "POST",
        "/api/notifications",
        {"name": "Upgrade test", "type": "webhook", "url": "json://127.0.0.1:9/upgrade-test", "events": OLD_EVENTS},
        expect=(200, 201),
    )

    log("running a backup with the old release")
    run = run_full_job(env, job_id)
    check(run.get("status") == "success", f"old release backup succeeded (status={run.get('status')})")
    old_dump = dump_path_of(run, env.pg)
    log(f"  ok: old release dumped {env.pg}/{PG_DB} to {old_dump}")
    snaps = [t.get("snapshot_id") for t in run.get("targets", []) if t.get("snapshot_id")]
    check(len(snaps) == 1, "old release recorded one snapshot")
    old_snapshot = snaps[0]
    check(old_snapshot in snapshot_ids(env), f"old release lists snapshot {old_snapshot}")

    state = {
        "api_key": setup["api_key"],
        "target_id": target_id,
        "job_id": job_id,
        "legacy_jobs": {t: i for t, i in by_type.items() if t != "full"},
        "channel_id": channel["id"],
        "old_run_id": run["id"],
        "old_snapshot": old_snapshot,
        "old_dump": old_dump,
        "sample": sample,
        "keyfile_sha": env.config_sha256(env.old, "/config/.keyfile"),
        "old_schema": env.schema_versions(env.old),
    }
    log(f"  old schema versions: {state['old_schema']['applied']}")

    log(f"stopping {env.old}")
    env.stop(env.old)
    # Keep the stopped container so its logs can be printed on failure.
    return state


def phase_new(env: Env, state: dict, marker: str) -> None:
    log(f"== phase 2: {env.new_image} on the same /config")
    env.api.api_key = ""
    env.start_arkive(env.new, env.new_image)

    # Starts: the keyfile guard ran and accepted the old keyfile.
    logs = env.docker.run("logs", env.new, check=False)
    output = (logs.stdout + logs.stderr).decode(errors="replace")
    check("Refusing to start" not in output, "new image started without a keyfile refusal")
    check(
        env.config_sha256(env.new, "/config/.keyfile") == state["keyfile_sha"],
        "the .keyfile is unchanged by the upgrade",
    )

    schema = env.schema_versions(env.new)
    check(
        set(range(2, schema["code"] + 1)) <= set(schema["applied"]) and max(schema["applied"]) == schema["code"],
        f"all migrations applied (code {schema['code']}, database {schema['applied']})",
    )
    check(max(schema["applied"]) > max(state["old_schema"]["applied"]), "the upgrade applied new migrations")

    status = env.api.ok("GET", "/api/status")
    check(status.get("setup_completed") is True, "status reports setup completed")
    session = env.api.ok("GET", "/api/auth/session")
    check(session.get("setup_required") is False, "new image does not ask for setup again")

    env.api.api_key = state["api_key"]
    env.api.ok("GET", "/api/jobs")
    log("  ok: the old API key is accepted")
    runs = items(env.api.ok("GET", "/api/jobs/runs"), "runs")
    check(any(r.get("id") == state["old_run_id"] for r in runs), "the old run is in the run history")

    # Migrations 5/6 extend subscriptions of channels that wanted failures.
    channels = items(env.api.ok("GET", "/api/notifications"), "channels")
    events = next((c.get("events") for c in channels if c.get("id") == state["channel_id"]), None)
    check(
        events is not None and set(OLD_EVENTS) | MIGRATED_EVENTS <= set(events),
        f"the old notification channel kept its events and gained the new failure events ({events})",
    )

    old_snapshot = state["old_snapshot"]
    target_id = state["target_id"]
    check(old_snapshot in snapshot_ids(env), f"old snapshot {old_snapshot} is listed from the cache")
    refresh = env.api.ok("POST", "/api/snapshots/refresh", timeout=300)
    check(
        not refresh.get("skipped") and refresh.get("refreshed", 0) >= 1, f"snapshot refresh read the repo ({refresh})"
    )
    check(old_snapshot in snapshot_ids(env), f"old snapshot {old_snapshot} is listed from the repository")

    # File restore from the old snapshot.
    restored = restore(env, old_snapshot, target_id, [SAMPLE_MOUNT])
    for rel, content in state["sample"].items():
        got = env.exec(env.new, "cat", str(restored / SAMPLE_MOUNT.lstrip("/") / rel))
        check(got == content, f"restored {rel} matches the original")

    # Database dump restore from the old snapshot.
    dump = restore_dump(env, old_snapshot, target_id, state["old_dump"])
    markers = load_dump_and_read_markers(env, env.new, dump)
    check(markers == [f"before-upgrade-{marker}"], f"old dump {Path(dump).name} restores with its rows ({markers})")

    # The restore drill on a snapshot written before dump manifests existed.
    drill = run_restore_drill(env)
    results = drill.get("results", [])
    log(f"  drill {drill.get('id')}: status={drill.get('status')} snapshot={drill.get('snapshot_id')} {results}")
    check(same_snapshot(drill.get("snapshot_id") or "", old_snapshot), "restore drill picked the old snapshot")
    check(
        drill.get("status") == "passed"
        and any(
            r.get("container_name") == env.pg and r.get("db_name") == PG_DB and r.get("status") == "passed"
            for r in results
        ),
        f"restore drill loaded the old {PG_DB} dump ({drill.get('error_message')!r})",
    )

    # A new backup, with a row written after the upgrade.
    env.psql(env.pg, f"INSERT INTO upgrade_marker (note) VALUES ('after-upgrade-{marker}')")  # nosec B608 - hex marker
    log("running a backup with the new image")
    run = run_full_job(env, state["job_id"])
    check(
        run.get("status") == "success",
        f"new backup succeeded (status={run.get('status')}, error={run.get('error_message')!r})",
    )
    new_dump = dump_path_of(run, env.pg)
    log(f"  ok: new backup dumped {env.pg}/{PG_DB} to {new_dump}")
    new_snaps = [t.get("snapshot_id") for t in run.get("targets", []) if t.get("snapshot_id")]
    check(len(new_snaps) == 1 and not same_snapshot(new_snaps[0], old_snapshot), "new backup wrote a new snapshot")
    new_snapshot = new_snaps[0]
    env.api.ok("POST", "/api/snapshots/refresh", timeout=300)
    listed = snapshot_ids(env)
    check({old_snapshot, new_snapshot} <= listed, "old and new snapshots are both in the repository")

    dump = restore_dump(env, new_snapshot, target_id, new_dump)
    markers = load_dump_and_read_markers(env, env.new, dump)
    check(
        markers == [f"before-upgrade-{marker}", f"after-upgrade-{marker}"],
        f"new dump {Path(dump).name} restores with the rows written after the upgrade",
    )

    # The other jobs the old setup created run too.
    for job_type, legacy_job in sorted(state["legacy_jobs"].items()):
        log(f"running the old {job_type} job with the new image")
        run = run_full_job(env, legacy_job)
        check(
            run.get("status") == "success",
            f"old {job_type} job succeeded (status={run.get('status')}, error={run.get('error_message')!r})",
        )

    status = env.api.ok("GET", "/api/status")
    log(f"  status after upgrade: status={status.get('status')} freshness={status.get('backup_freshness')}")
    check(status.get("status") == "ok", f"status is ok after the upgrade ({status.get('status')})")

    # The guard passed above because the keyfile matches, not because it
    # found nothing to check: without it the upgraded database is refused.
    log("checking the keyfile guard on the upgraded database")
    env.stop(env.new)
    env.in_config("mv /config/.keyfile /config/.keyfile.upgrade-test")
    try:
        env.start_arkive(env.probe, env.new_image, serve=False)
        output = env.wait_exit(env.probe)
    finally:
        env.in_config("mv /config/.keyfile.upgrade-test /config/.keyfile")
    check(
        "Refusing to start" in output and ".keyfile is missing" in output,
        "without its .keyfile the upgraded database is refused",
    )
    env.docker.run("start", env.new)
    env.wait_ready(env.new)
    env.api.ok("GET", "/api/jobs")
    log("  ok: with the .keyfile back, the upgraded install starts again")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upgrade an Arkive install made by an old release to an image built from this tree.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--old-image", default=DEFAULT_OLD_IMAGE, help="release to upgrade from")
    parser.add_argument(
        "--new-image",
        default="",
        help="image to upgrade to (default: build one from this checkout and remove it afterwards)",
    )
    parser.add_argument(
        "--docker",
        choices=("dind", "host"),
        default="dind",
        help="dind: private privileged Docker daemon (safe anywhere); host: the current daemon "
        "(throwaway machines only, refuses when other containers exist)",
    )
    parser.add_argument("--dind-image", default=DEFAULT_DIND_IMAGE)
    parser.add_argument("--postgres-image", default=DEFAULT_POSTGRES_IMAGE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="127.0.0.1 port for the Arkive API")
    parser.add_argument("--name-prefix", default=DEFAULT_PREFIX, help="prefix of every container/volume created")
    parser.add_argument("--run-timeout", type=float, default=600, help="seconds to wait for each backup run")
    parser.add_argument("--keep", action="store_true", help="leave containers and volumes in place for debugging")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*-", args.name_prefix):
        parser.error("--name-prefix must be lowercase [a-z0-9_.-] and end with '-'")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if shutil.which("docker") is None:
        print("docker CLI not found", file=sys.stderr)
        return 2
    env = Env(args)
    marker = secrets.token_hex(4)
    started = time.monotonic()
    log(f"API on 127.0.0.1:{env.port}, docker mode {env.mode}, names {env.prefix}*")
    try:
        env.prepare()
        state = phase_old(env, marker)
        phase_new(env, state, marker)
    except UpgradeCheckError as e:
        log(f"FAIL: {e}")
        try:
            env.print_logs()
        except Exception as log_error:  # noqa: BLE001 - best effort while failing
            log(f"could not read container logs: {log_error}")
        log(f"FAIL: {e}")
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    finally:
        env.cleanup()
    log(f"PASS: upgrade from {args.old_image} to {env.new_image} ({time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
