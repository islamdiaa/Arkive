"""Helpers of scripts/upgrade-path-test.py (the Docker-based upgrade test).

The script itself needs Docker and runs as ``make test-upgrade``; these tests
cover the parts that keep it safe and correct without Docker: refusing a
shared daemon, name-prefix validation, and how results are matched.
"""

import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

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


class FakeDocker:
    """Records docker CLI calls; ``ps`` lists ``containers``."""

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
        if args[0] == "rm" or args[:2] == ("volume", "rm"):
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


def test_host_mode_accepts_a_daemon_with_only_its_own_containers():
    names = ["arkive-upgrade-test-postgres", "arkive-upgrade-test-arkive-old", ""]
    assert upgrade.foreign_containers(names, "arkive-upgrade-test-") == []
    assert upgrade.foreign_containers(["tower-db", *names], "arkive-upgrade-test-") == ["tower-db"]


def test_defaults_isolate_the_test_and_names_share_the_prefix():
    args = upgrade.parse_args([])
    assert args.docker == "dind"
    assert args.port == 8310
    env = upgrade.Env(upgrade.parse_args(["--name-prefix", "arkive-wf-upgrade-test-"]))
    names = [env.dind, env.dind_volume, *env.containers, *env.volumes.values()]
    assert all(n.startswith("arkive-wf-upgrade-test-") for n in names)
    assert env.new_image.startswith("arkive-wf-upgrade-test-")


@pytest.mark.parametrize("prefix", ["Arkive-", "arkive", "arkive test-", "-arkive-"])
def test_invalid_name_prefixes_are_rejected(prefix):
    with pytest.raises(SystemExit):
        upgrade.parse_args(["--name-prefix", prefix])


def test_remove_all_retries_until_objects_are_gone(monkeypatch):
    monkeypatch.setattr(upgrade.time, "sleep", lambda _s: None)
    fake = FakeDocker(existing={"t-dind", "t-dind-lib"}, failures={"t-dind": 1, "t-dind-lib": 1})

    assert upgrade.remove_all(fake, [("container", "t-dind"), ("volume", "t-dind-lib")]) == []
    assert fake.existing == set()


def test_remove_all_reports_leftovers(monkeypatch):
    monkeypatch.setattr(upgrade.time, "sleep", lambda _s: None)
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
