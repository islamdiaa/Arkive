"""Helpers of scripts/upgrade-path-test.py (the Docker-based upgrade test).

The script itself needs Docker and runs as ``make test-upgrade``; these tests
cover the parts that keep it safe and correct without Docker: refusing a
shared daemon, claiming the name prefix, keeping the API off shared networks,
name-prefix validation, retrying requests that hit the backup lock, the fake
Unraid flash, and how results are matched.
"""

import gzip
import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from app.core.platform import _looks_like_unraid_flash

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "upgrade-path-test.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("upgrade_path_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Don't leave a __pycache__ next to the script in the checkout.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


upgrade = _load_script()


class FakeClock:
    """Stands in for the script's ``time`` module (patched on the script only,
    so other code in the worker keeps the real clock): sleeping advances it."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    @staticmethod
    def strftime(fmt):
        return "00:00:00"


class FakeDocker:
    """Records docker CLI calls; ``ps`` lists ``containers``; ``existing`` holds
    the names of objects that exist (creating a network that exists fails, as
    it does in Docker; creating a volume that exists silently succeeds)."""

    def __init__(self, containers=(), existing=(), failures=None):
        self.containers = list(containers)
        self.existing = set(existing)
        self.failures = dict(failures or {})
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, check=True, input=None, timeout=300):
        self.calls.append(args)
        if args[:1] == ("ps",):
            out = "\n".join(self.containers).encode()
            return subprocess.CompletedProcess(args, 0, out, b"")
        name = args[-1]
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            return subprocess.CompletedProcess(args, 1, b"", b"busy")
        if args[:2] == ("network", "create") and name in self.existing:
            return subprocess.CompletedProcess(args, 1, b"", f"network with name {name} already exists".encode())
        if args[:2] in (("network", "create"), ("volume", "create")):
            self.existing.add(name)
        if args[0] == "rm" or args[:2] in (("volume", "rm"), ("network", "rm"), ("image", "rm")):
            self.existing.discard(name)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def out(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout.decode()

    def exists(self, kind, name):
        return name in self.existing


def test_host_mode_refuses_a_daemon_with_other_containers(monkeypatch):
    fake = FakeDocker(containers=["paperless-db-1", "arkive-upgrade-test-postgres"])
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--docker", "host", "--new-image", "arkive:test"]))

    with pytest.raises(upgrade.UpgradeCheckError, match="paperless-db-1"):
        env.prepare()

    # Refused before building, pulling or creating anything.
    assert all(call[0] in ("ps",) for call in fake.calls)


def test_cleanup_leaves_objects_that_existed_before_the_run(monkeypatch):
    fake = FakeDocker(existing={"arkive-upgrade-test-dind"})
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--new-image", "arkive:test"]))

    with pytest.raises(upgrade.UpgradeCheckError, match="already exists"):
        env.prepare()
    env.cleanup()

    assert fake.existing == {"arkive-upgrade-test-dind"}
    assert not [c for c in fake.calls if c[0] in ("rm", "stop", "build") or c[:2] == ("volume", "rm")]


def _creates_and_removals(calls):
    return [c for c in calls if c[0] in ("build", "rm", "stop", "run") or c[1:2] in (("create",), ("rm",))]


def test_a_second_run_with_the_same_prefix_stops_without_touching_the_first(monkeypatch):
    # The first run has claimed the prefix (its network) but hasn't created
    # its volumes yet, so the name checks alone would pass.
    fake = FakeDocker(existing={"arkive-upgrade-test-net"})
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--new-image", "arkive:test"]))

    with pytest.raises(upgrade.UpgradeCheckError, match="another run with this --name-prefix"):
        env.prepare()
    env.cleanup()

    assert not env.owns_names
    assert fake.existing == {"arkive-upgrade-test-net"}
    # Nothing but the failed claim: no build, no volumes, no removals.
    assert _creates_and_removals(fake.calls) == [("network", "create", "arkive-upgrade-test-net")]


def test_prepare_claims_the_prefix_before_building_and_cleanup_releases_it_last(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--docker", "host"]))

    env.prepare()

    claim = fake.calls.index(("network", "create", "arkive-upgrade-test-net"))
    build = next(i for i, c in enumerate(fake.calls) if c[0] == "build")
    first_volume = next(i for i, c in enumerate(fake.calls) if c[:2] == ("volume", "create"))
    assert claim < build < first_volume
    assert env.owns_names and env.built_new_image

    fake.calls.clear()
    env.cleanup()

    removals = [c for c in _creates_and_removals(fake.calls) if c[0] != "build"]
    assert removals[-2:] == [("network", "rm", "arkive-upgrade-test-net"), ("image", "rm", env.new_image)]
    assert ("volume", "rm", "-f", "arkive-upgrade-test-flash") in removals
    assert fake.existing == set()


def test_prepare_refuses_to_reuse_an_image_tag_it_would_build_and_remove(monkeypatch):
    fake = FakeDocker(existing={"arkive-upgrade-test-new:local"})
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args([]))

    with pytest.raises(upgrade.UpgradeCheckError, match="image arkive-upgrade-test-new:local already exists"):
        env.prepare()
    env.cleanup()

    assert fake.existing == {"arkive-upgrade-test-new:local"}
    assert _creates_and_removals(fake.calls) == []


def test_private_daemon_is_on_its_own_network_and_published_on_loopback_only(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--new-image", "arkive:test"]))
    env.docker = fake  # answers "docker info" for the private daemon

    env._start_private_daemon()

    run = next(c for c in fake.calls if c[0] == "run")
    assert run[run.index("--network") + 1] == "arkive-upgrade-test-net"
    assert run[run.index("-p") + 1] == "127.0.0.1:8310:8200"


@pytest.mark.parametrize(
    ("mode", "network", "publish"),
    [("dind", None, "8200:8200"), ("host", "arkive-upgrade-test-net", "127.0.0.1:8310:8200")],
)
def test_arkive_runs_like_the_unraid_template_with_the_api_off_shared_networks(monkeypatch, mode, network, publish):
    fake = FakeDocker()
    monkeypatch.setattr(upgrade, "HOST", fake)
    env = upgrade.Env(upgrade.parse_args(["--docker", mode, "--new-image", "arkive:test"]))
    env.docker = fake
    monkeypatch.setattr(env, "wait_ready", lambda name: {})

    env.start_arkive(env.old, "arkive:old")

    run = fake.calls[-1]
    # In dind mode only test containers share the private daemon; the
    # daemon's own container is the one on the dedicated network.
    assert (run[run.index("--network") + 1] if "--network" in run else None) == network
    assert run[run.index("-p") + 1] == publish
    assert "arkive-upgrade-test-flash:/boot-config:ro" in run
    assert run[run.index("--user") + 1] == "0:0"


def test_fake_flash_makes_arkive_detect_unraid(tmp_path):
    for name, content in upgrade.fake_flash("abc").items():
        (tmp_path / name).write_bytes(content)
    assert _looks_like_unraid_flash(tmp_path)


def _flash_archive(files, gz):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in files.items():
            # As ``tar -C /boot-config .`` names them.
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    raw = buffer.getvalue()
    return gzip.compress(raw) if gz else raw


@pytest.mark.parametrize("gz", [True, False], ids=["0.2.4-tar.gz", "tar"])
def test_flash_archive_member_reads_old_and_new_archives(gz):
    files = upgrade.fake_flash("abc")
    raw = _flash_archive(files, gz)

    assert {name: upgrade.flash_archive_member(raw, name) for name in files} == files
    assert upgrade.flash_archive_member(raw, "super.dat") is None


def test_newest_flash_archive_picks_by_suffix_and_timestamp():
    names = [
        "flash_20261110_120000.tar.gz",
        "flash_20261110_125900.tar",
        "flash_20261110_130000.tar",
        "postgres_appdb_1a2b.sql",
    ]
    assert upgrade.newest_flash_archive(names, ".tar") == "/config/dumps/flash_20261110_130000.tar"
    assert upgrade.newest_flash_archive(names, ".tar.gz") == "/config/dumps/flash_20261110_120000.tar.gz"
    with pytest.raises(upgrade.UpgradeCheckError, match="no flash_"):
        upgrade.newest_flash_archive(names[3:], ".tar")


class ScriptedApi(upgrade.Api):
    def __init__(self, responses):
        super().__init__("http://127.0.0.1:8310")
        self.responses = list(responses)
        self.calls = 0

    def call(self, method, path, body=None, timeout=120):
        self.calls += 1
        return self.responses.pop(0)


def test_job_trigger_retries_while_the_previous_run_still_holds_the_lock(monkeypatch):
    # The previous run's status is committed before it releases backup.lock.
    monkeypatch.setattr(upgrade, "time", FakeClock())
    busy = (409, {"detail": "Another backup is already running (run_id=abc)"})
    api = ScriptedApi([busy, busy, (202, {"run_id": "r2"})])

    assert api.ok("POST", "/api/jobs/j/run", expect=(200, 202), conflict_wait=120) == {"run_id": "r2"}
    assert api.calls == 3


def test_conflict_retry_gives_up_at_the_deadline(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(upgrade, "time", clock)
    api = ScriptedApi([(409, {"detail": "busy"})] * 1000)

    with pytest.raises(upgrade.UpgradeCheckError, match="HTTP 409"):
        api.ok("POST", "/api/restore", {}, conflict_wait=120)
    assert api.calls > 1
    assert 120 <= clock.now < 125


def test_without_conflict_wait_a_409_fails_at_once():
    api = ScriptedApi([(409, {"detail": "busy"}), (200, {})])

    with pytest.raises(upgrade.UpgradeCheckError, match="HTTP 409"):
        api.ok("POST", "/api/drills/run", expect=(202,))
    assert api.calls == 1


def test_host_mode_accepts_a_daemon_with_only_its_own_containers():
    names = ["arkive-upgrade-test-postgres", "arkive-upgrade-test-arkive-old", ""]
    assert upgrade.foreign_containers(names, "arkive-upgrade-test-") == []
    assert upgrade.foreign_containers(["tower-db", *names], "arkive-upgrade-test-") == ["tower-db"]


def test_defaults_isolate_the_test_and_names_share_the_prefix():
    args = upgrade.parse_args([])
    assert args.docker == "dind"
    assert args.port == 8310
    env = upgrade.Env(upgrade.parse_args(["--name-prefix", "arkive-wf-upgrade-test-"]))
    names = [env.dind, env.dind_volume, env.network, *env.containers, *env.volumes.values()]
    assert all(n.startswith("arkive-wf-upgrade-test-") for n in names)
    assert env.new_image.startswith("arkive-wf-upgrade-test-")


@pytest.mark.parametrize("prefix", ["Arkive-", "arkive", "arkive test-", "-arkive-"])
def test_invalid_name_prefixes_are_rejected(prefix):
    with pytest.raises(SystemExit):
        upgrade.parse_args(["--name-prefix", prefix])


def test_remove_all_retries_until_objects_are_gone(monkeypatch):
    monkeypatch.setattr(upgrade, "time", FakeClock())
    fake = FakeDocker(existing={"t-dind", "t-dind-lib"}, failures={"t-dind": 1, "t-dind-lib": 1})

    assert upgrade.remove_all(fake, [("container", "t-dind"), ("volume", "t-dind-lib")]) == []
    assert fake.existing == set()


def test_remove_all_reports_leftovers(monkeypatch):
    monkeypatch.setattr(upgrade, "time", FakeClock())
    fake = FakeDocker(existing={"t-dind"}, failures={"t-dind": 99})

    assert upgrade.remove_all(fake, [("container", "t-dind")], attempts=3) == ["container t-dind"]


def test_sample_tar_holds_the_files_readable_by_anyone():
    files = {"notes.txt": b"hello\n", "nested/blob.bin": b"\x00\x01"}
    with tarfile.open(fileobj=io.BytesIO(upgrade.sample_tar(files))) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert members["nested"].isdir() and members["nested"].mode == 0o755
        for name, content in files.items():
            assert members[name].mode == 0o644
            assert tar.extractfile(name).read() == content


def test_snapshot_ids_match_short_and_full_forms():
    full = "ec050f24b747f5142e42bdb0126dae35c5eb2b1036a483b4ae21ac24dabba690"
    assert upgrade.same_snapshot(full, "ec050f24")
    assert upgrade.same_snapshot("ec050f24", full)
    assert not upgrade.same_snapshot(full, "32a1f77e")
    assert not upgrade.same_snapshot("", full)


def test_dump_path_of_picks_the_successful_dump_of_the_test_database():
    run = {
        "id": "r1",
        "databases": [
            {"container_name": "pg", "db_name": "globals", "status": "success", "host_path": "/config/dumps/g.sql"},
            {"container_name": "pg", "db_name": "appdb", "status": "failed", "host_path": "/config/dumps/old.sql"},
            {"container_name": "pg", "db_name": "appdb", "status": "success", "host_path": "/config/dumps/a.sql"},
        ],
    }
    assert upgrade.dump_path_of(run, "pg") == "/config/dumps/a.sql"
    with pytest.raises(upgrade.UpgradeCheckError):
        upgrade.dump_path_of(run, "other")


def test_dump_path_of_rejects_a_path_outside_the_dump_dir():
    run = {
        "id": "r1",
        "databases": [{"container_name": "pg", "db_name": "appdb", "status": "success", "host_path": ""}],
    }
    with pytest.raises(upgrade.UpgradeCheckError, match="unexpected dump path"):
        upgrade.dump_path_of(run, "pg")


def test_items_reads_lists_and_wrapped_lists():
    assert upgrade.items([1]) == [1]
    assert upgrade.items({"items": [2]}) == [2]
    assert upgrade.items({"jobs": [3], "items": [4]}, "jobs") == [3]
    with pytest.raises(upgrade.UpgradeCheckError):
        upgrade.items({"detail": "nope"})
