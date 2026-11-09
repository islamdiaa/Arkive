"""3-2-1 awareness: detecting when every backup target is on this server,
reporting it in /api/status, and telling the user once."""

import ipaddress
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import app.services.scheduler as scheduler_mod
from app.core.database import init_db, run_migrations
from app.core.security import _reset_fernet, encrypt_config
from app.services import offsite
from app.services.notification_events import DEFAULT_EVENTS, EVENT_TYPES
from app.utils.mounts import MountEntry
from tests.conftest import auth_headers, do_setup

FIB_TRIE = """\
Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.0
              /8 host LOCAL
           |-- 127.0.0.1
              /32 host LOCAL
     +-- 172.18.0.0/16 2 0 2
        |-- 172.18.0.5
           /32 host LOCAL
        |-- 172.18.255.255
           /32 link BROADCAST
"""

IF_INET6 = "00000000000000000000000000000001 01 80 10 80       lo\n"

NETWORK_CFG = """\
USE_DHCP[0]="no"
IPADDR[0]="192.168.1.100"
NETMASK[0]="255.255.255.0"
GATEWAY="192.168.1.1"
"""

MOUNTINFO = """\
100 90 0:40 / / rw,relatime - overlay overlay rw
102 100 0:50 /appdata/arkive /config rw,noatime - shfs shfs rw
104 100 0:50 /backups /mnt/user/backups rw,noatime - shfs shfs rw
105 100 0:60 / /mnt/remotes/nas rw,relatime - cifs //nas/backup rw
106 100 0:61 / /mnt/disks/usb rw,relatime - xfs /dev/sdx1 rw
"""


def _ip(text: str):
    return ipaddress.ip_address(text)


@pytest.fixture(autouse=True)
def _isolated_identity(tmp_path, monkeypatch):
    """Point the identity readers at fixtures instead of this machine."""
    proc_net = tmp_path / "proc_net"
    proc_net.mkdir()
    (proc_net / "fib_trie").write_text(FIB_TRIE)
    (proc_net / "if_inet6").write_text(IF_INET6)
    boot = tmp_path / "boot-config"
    boot.mkdir()
    (boot / "network.cfg").write_text(NETWORK_CFG)
    (boot / "ident.cfg").write_text('NAME="tower"\n')
    monkeypatch.setattr(offsite, "PROC_NET", proc_net)
    monkeypatch.setattr(offsite, "BOOT_CONFIG", boot)
    monkeypatch.setattr(offsite.socket, "gethostname", lambda: "0123456789ab")
    offsite.reset_identity_cache()
    yield
    offsite.reset_identity_cache()
    _reset_fernet()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_type,config,expected",
    [
        ("sftp", {"host": "tower"}, "tower"),
        ("sftp", {"host": " tower:2222 "}, "tower"),
        ("sftp", {"host": "[::1]"}, "::1"),
        ("sftp", {"host": "fe80::1"}, "fe80::1"),
        ("sftp", {}, ""),
        ("s3", {"endpoint": "http://minio:9000"}, "minio"),
        ("s3", {"endpoint": "minio:9000"}, "minio"),
        ("s3", {"endpoint": "https://s3.us-west-2.amazonaws.com"}, "s3.us-west-2.amazonaws.com"),
        ("s3", {"endpoint": "192.168.1.100:9000"}, "192.168.1.100"),
        ("s3", {"endpoint": "http://[::1]:9000"}, "::1"),
        ("b2", {"bucket": "x"}, ""),
        ("local", {"path": "/mnt/user/backups"}, ""),
    ],
)
def test_target_host(target_type, config, expected):
    assert offsite.target_host(target_type, config) == expected


IDENTITY = offsite.LocalIdentity(
    names=frozenset({"tower"}),
    addresses=frozenset({_ip("172.17.0.1"), _ip("192.168.1.100")}),
    containers=frozenset({"minio"}),
)


@pytest.mark.parametrize(
    "host,local",
    [
        ("localhost", True),
        ("backup.localhost", True),
        ("127.0.0.1", True),
        ("127.0.1.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("0.0.0.0", True),
        ("host.docker.internal", True),
        ("tower", True),
        ("TOWER.local", True),
        ("tower.home.arpa", True),
        ("tower.example.com", False),
        ("192.168.1.100", True),
        ("172.17.0.1", True),
        ("192.168.1.50", False),
        ("minio", True),
        ("nas", False),
        ("s3.amazonaws.com", False),
        ("", False),
    ],
)
def test_host_locality(host, local):
    assert (offsite.host_locality(host, IDENTITY) is not None) is local


def test_local_path_locality_treats_network_shares_as_offsite():
    entries = [
        MountEntry(root="/", mount_point="/", fstype="overlay"),
        MountEntry(root="/backups", mount_point="/mnt/user/backups", fstype="shfs"),
        MountEntry(root="/", mount_point="/mnt/remotes/nas", fstype="cifs"),
        MountEntry(root="/", mount_point="/mnt/remotes/nfs", fstype="nfs4"),
    ]
    assert offsite.local_path_locality("/mnt/user/backups/arkive", entries)
    assert offsite.local_path_locality("/mnt/remotes/nas/arkive", entries) is None
    assert offsite.local_path_locality("/mnt/remotes/nfs", entries) is None
    # Unknown path: a local target is on this server unless shown otherwise.
    assert offsite.local_path_locality("", entries) == "a folder on this server"


def test_interface_and_unraid_addresses(tmp_path):
    addresses = offsite._interface_addresses(offsite.PROC_NET)
    assert _ip("172.18.0.5") in addresses and _ip("127.0.0.1") in addresses and _ip("::1") in addresses
    assert _ip("172.18.255.255") not in addresses  # broadcast, not an interface address
    assert offsite._unraid_addresses(offsite.BOOT_CONFIG) == {_ip("192.168.1.100")}
    assert offsite._interface_addresses(tmp_path / "missing") == set()
    assert offsite._unraid_addresses(tmp_path / "missing") == set()


def _network(driver: str, gateway: str | None):
    config = [{"Subnet": "10.0.0.0/24", "Gateway": gateway}] if gateway else []
    return SimpleNamespace(attrs={"Driver": driver, "IPAM": {"Config": config}})


def test_docker_identity_uses_bridge_gateways_only():
    client = MagicMock()
    client.info.return_value = {"Name": "Tower"}
    client.networks.list.return_value = [
        _network("bridge", "172.17.0.1"),
        _network("bridge", "172.20.0.1"),
        _network("macvlan", "192.168.1.1"),  # the LAN router, not this server
        _network("host", None),
    ]
    names, addresses = offsite._docker_identity(client)
    assert names == {"tower"}
    assert addresses == {_ip("172.17.0.1"), _ip("172.20.0.1")}


def test_docker_identity_survives_daemon_errors():
    client = MagicMock()
    client.info.side_effect = RuntimeError("daemon down")
    client.networks.list.side_effect = RuntimeError("daemon down")
    assert offsite._docker_identity(client) == (set(), set())
    assert offsite._docker_identity(None) == (set(), set())


def test_system_identity_is_cached_and_never_stacks_gathers(monkeypatch):
    calls = []

    def gather(docker_client=None):
        calls.append(docker_client)
        return offsite.LocalIdentity(names=frozenset({"tower"}))

    monkeypatch.setattr(offsite, "gather_system_identity", gather)
    first = offsite.cached_system_identity()
    assert offsite.cached_system_identity() is first
    assert len(calls) == 1

    # While a gather is in flight (e.g. stuck on Docker), callers get the
    # last result instead of starting another one.
    offsite.reset_identity_cache()
    lock: threading.Lock = offsite._identity_lock
    with lock:
        assert offsite.cached_system_identity() is None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# evaluate_offsite
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "arkive.db"
    await init_db(path)
    await run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        yield conn


async def _add_target(db, config_dir, target_id, target_type, config, enabled=1, encrypted=True):
    raw = encrypt_config(config, str(config_dir)) if encrypted else json.dumps(config)
    await db.execute(
        "INSERT INTO storage_targets (id, name, type, enabled, config) VALUES (?, ?, ?, ?, ?)",
        (target_id, f"Target {target_id}", target_type, enabled, raw),
    )
    await db.commit()


async def _evaluate(db, tmp_path, **kwargs):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO)
    return await offsite.evaluate_offsite(db, config_dir=tmp_path, mountinfo=mountinfo, **kwargs)


async def test_no_targets_is_not_a_warning(db, tmp_path):
    result = await _evaluate(db, tmp_path)
    assert result == {
        "has_offsite_copy": False,
        "warning": False,
        "enabled_targets": 0,
        "offsite_targets": 0,
        "local_targets": [],
        "message": None,
    }


async def test_only_local_targets_warn(db, tmp_path):
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    await _add_target(db, tmp_path, "share", "local", {"path": "/mnt/user/backups"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True and result["has_offsite_copy"] is False
    assert result["enabled_targets"] == 2 and result["offsite_targets"] == 0
    assert {t["id"] for t in result["local_targets"]} == {"usb", "share"}
    assert all(t["type"] == "local" and "on this server" in t["reason"] for t in result["local_targets"])
    assert "no offsite copy" in result["message"]


async def test_cloud_target_is_offsite(db, tmp_path):
    await _add_target(db, tmp_path, "share", "local", {"path": "/mnt/user/backups"})
    await _add_target(db, tmp_path, "b2t", "b2", {"bucket": "b", "key_id": "k", "app_key": "s"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is False and result["has_offsite_copy"] is True
    assert result["offsite_targets"] == 1 and [t["id"] for t in result["local_targets"]] == ["share"]


async def test_disabled_offsite_target_does_not_count(db, tmp_path):
    await _add_target(db, tmp_path, "share", "local", {"path": "/mnt/user/backups"})
    await _add_target(db, tmp_path, "b2t", "b2", {"bucket": "b"}, enabled=0)
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True and result["enabled_targets"] == 1


async def test_local_target_on_network_share_is_offsite(db, tmp_path):
    await _add_target(db, tmp_path, "nas", "local", {"path": "/mnt/remotes/nas/arkive"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is False and result["has_offsite_copy"] is True


@pytest.mark.parametrize(
    "target_type,config,local",
    [
        ("sftp", {"host": "localhost", "username": "u"}, True),
        ("sftp", {"host": "tower.local", "username": "u"}, True),  # Unraid ident.cfg name
        ("sftp", {"host": "192.168.1.100", "username": "u"}, True),  # Unraid network.cfg address
        ("sftp", {"host": "172.18.0.5", "username": "u"}, True),  # an interface address
        ("sftp", {"host": "backup.example.com", "username": "u"}, False),
        ("sftp", {"host": "192.168.1.50", "username": "u"}, False),
        ("s3", {"endpoint": "http://127.0.0.1:9000", "bucket": "b"}, True),
        ("s3", {"endpoint": "http://host.docker.internal:9000", "bucket": "b"}, True),
        ("s3", {"endpoint": "https://s3.eu-central-1.amazonaws.com", "bucket": "b"}, False),
        ("wasabi", {"bucket": "b", "region": "eu-central-1"}, False),
    ],
)
async def test_self_hosted_targets_pointing_back_here(db, tmp_path, target_type, config, local):
    await _add_target(db, tmp_path, "t1", target_type, config)
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is local
    assert result["has_offsite_copy"] is not local


async def test_s3_on_a_local_container_or_compose_service(db, tmp_path):
    await db.execute(
        "INSERT INTO discovered_containers (name, image, compose_project) VALUES "
        "('minio', 'minio/minio', NULL), ('storage-garage-1', 'dxflrs/garage', 'storage')"
    )
    await db.commit()
    await _add_target(db, tmp_path, "m1", "s3", {"endpoint": "http://minio:9000", "bucket": "b"})
    await _add_target(db, tmp_path, "g1", "s3", {"endpoint": "http://garage:3900", "bucket": "b"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True
    reasons = {t["id"]: t["reason"] for t in result["local_targets"]}
    assert reasons == {
        "m1": "minio is a container on this server",
        "g1": "garage is a container on this server",
    }


async def test_server_name_and_docker_bridge_gateway(db, tmp_path):
    await db.execute("INSERT INTO settings (key, value) VALUES ('server_name', 'unraid-test')")
    await db.commit()
    client = MagicMock()
    client.info.return_value = {"Name": "test-server"}
    client.networks.list.return_value = [_network("bridge", "172.17.0.1")]
    await _add_target(db, tmp_path, "a", "sftp", {"host": "unraid-test", "username": "u"})
    await _add_target(db, tmp_path, "b", "sftp", {"host": "172.17.0.1", "username": "u"})
    await _add_target(db, tmp_path, "c", "sftp", {"host": "test-server.lan", "username": "u"})
    result = await _evaluate(db, tmp_path, docker_client=client)
    assert result["warning"] is True and len(result["local_targets"]) == 3


async def test_unreadable_remote_config_is_not_claimed_local(db, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    await _add_target(db, other, "s1", "sftp", {"host": "localhost", "username": "u"})
    await _add_target(db, other, "l1", "local", {"path": "/mnt/remotes/nas"})
    (tmp_path / ".keyfile").unlink(missing_ok=True)
    result = await _evaluate(db, tmp_path)
    # The sftp host can't be read, so it isn't evidently local; the local
    # target is local by type even without its path.
    assert result["offsite_targets"] == 1 and [t["id"] for t in result["local_targets"]] == ["l1"]
    assert not (tmp_path / ".keyfile").exists(), "reading configs must never create a keyfile"


async def test_plain_json_legacy_config(db, tmp_path):
    await _add_target(db, tmp_path, "s1", "sftp", {"host": "127.0.0.1", "username": "u"}, encrypted=False)
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True


async def test_slow_identity_does_not_stall(db, tmp_path, monkeypatch):
    def hang(docker_client=None):
        threading.Event().wait(2)
        return None

    monkeypatch.setattr(offsite, "cached_system_identity", hang)
    monkeypatch.setattr(offsite, "IDENTITY_TIMEOUT_SECONDS", 0.05)
    await _add_target(db, tmp_path, "s1", "sftp", {"host": "localhost", "username": "u"})
    await _add_target(db, tmp_path, "s2", "sftp", {"host": "192.168.1.100", "username": "u"})
    result = await _evaluate(db, tmp_path)
    # Loopback is still recognised; the host address needs the identity.
    assert [t["id"] for t in result["local_targets"]] == ["s1"]


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


def _app(client):
    return client._transport.app


async def _insert_target(client, target_id, target_type, config, enabled=1):
    app = _app(client)
    async with aiosqlite.connect(app.state.config.db_path) as conn:
        await conn.execute(
            "INSERT INTO storage_targets (id, name, type, enabled, config, status) VALUES (?, ?, ?, ?, ?, 'healthy')",
            (
                target_id,
                target_id.upper(),
                target_type,
                enabled,
                encrypt_config(config, str(app.state.config.config_dir)),
            ),
        )
        await conn.commit()


async def test_status_reports_missing_offsite_copy_as_warning_only(client):
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    async with aiosqlite.connect(_app(client).state.config.db_path) as conn:
        await conn.execute("DELETE FROM storage_targets")
        await conn.commit()
    await _insert_target(client, "usb", "local", {"path": "/mnt/disks/usb"})

    before = (await client.get("/api/status", headers=headers)).json()
    assert before["offsite"]["warning"] is True
    assert before["offsite"]["has_offsite_copy"] is False
    assert before["offsite"]["local_targets"][0]["id"] == "usb"
    assert before["offsite"]["message"]

    await _insert_target(client, "b2t", "b2", {"bucket": "b", "key_id": "k", "app_key": "s"})
    after = (await client.get("/api/status", headers=headers)).json()
    assert after["offsite"]["warning"] is False and after["offsite"]["has_offsite_copy"] is True
    # The warning never changes the overall verdict.
    assert before["status"] == after["status"]


async def test_status_offsite_is_not_in_unauthenticated_payload(client):
    await do_setup(client)
    body = (await client.get("/api/status")).json()
    assert "offsite" not in body


async def test_status_survives_offsite_check_failure(client, monkeypatch):
    import app.api.status as status_api

    data = await do_setup(client)
    monkeypatch.setattr(status_api, "evaluate_offsite", AsyncMock(side_effect=RuntimeError("boom")))
    resp = await client.get("/api/status", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200 and resp.json()["offsite"] is None


# ---------------------------------------------------------------------------
# Freshness job: tell the user once
# ---------------------------------------------------------------------------


def test_event_is_in_catalog_and_defaults():
    assert "backup.no_offsite" in EVENT_TYPES and "backup.no_offsite" in DEFAULT_EVENTS


class TestOffsiteNotification:
    async def _seed(self, tmp_path, *targets):
        path = tmp_path / "arkive.db"
        await init_db(path)
        await run_migrations(path)
        async with aiosqlite.connect(path) as conn:
            for target_id, target_type, config in targets:
                await _add_target(conn, tmp_path, target_id, target_type, config)
        return path

    def _scheduler(self, tmp_path, notifier):
        config = SimpleNamespace(db_path=tmp_path / "arkive.db", config_dir=tmp_path, dump_dir=tmp_path / "dumps")
        return scheduler_mod.ArkiveScheduler(orchestrator=MagicMock(), config=config, notifier=notifier)

    @staticmethod
    def _notifier(status="sent"):
        notifier = MagicMock()
        notifier.send = AsyncMock(return_value=[{"channel_id": "c1", "status": status}])
        return notifier

    @staticmethod
    async def _activity(path, action):
        async with aiosqlite.connect(path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM activity_log WHERE type='target' AND action=?", (action,))
            return (await cursor.fetchone())[0]

    async def test_notifies_once_across_restarts(self, tmp_path, monkeypatch):
        path = await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        ping = AsyncMock(return_value={"ok": True, "status_code": 200, "error": None})
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", ping)
        notifier = self._notifier()

        await self._scheduler(tmp_path, notifier)._run_freshness_check()
        await self._scheduler(tmp_path, notifier)._run_freshness_check()  # a new process

        assert notifier.send.await_count == 1
        args, kwargs = notifier.send.await_args
        assert args[0] == "backup.no_offsite" and kwargs["severity"] == "warning"
        assert "Target usb" in args[2]
        assert await self._activity(path, "no_offsite") == 1

    async def test_does_not_affect_heartbeat(self, tmp_path, monkeypatch):
        await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        async with aiosqlite.connect(tmp_path / "arkive.db") as conn:
            await conn.execute("INSERT INTO settings (key, value) VALUES ('heartbeat_url', 'https://hc.example.com/x')")
            await conn.commit()
        monkeypatch.setattr(scheduler_mod, "evaluate_freshness", AsyncMock(return_value=_healthy_freshness()))
        ping = AsyncMock(return_value={"ok": True, "status_code": 200, "error": None})
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", ping)
        await self._scheduler(tmp_path, self._notifier())._run_freshness_check()
        ping.assert_awaited_once()

    async def test_retries_until_a_channel_receives_it(self, tmp_path, monkeypatch):
        path = await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        notifier = self._notifier()
        notifier.send.return_value = []  # no channel subscribed yet
        sched = self._scheduler(tmp_path, notifier)
        await sched._run_freshness_check()
        notifier.send.return_value = [{"channel_id": "c1", "status": "failed"}]
        await sched._run_freshness_check()
        notifier.send.return_value = [{"channel_id": "c1", "status": "sent"}]
        await sched._run_freshness_check()
        await sched._run_freshness_check()
        assert notifier.send.await_count == 3
        assert await self._activity(path, "no_offsite") == 1

    async def test_resolving_resets_so_a_regression_is_told_again(self, tmp_path, monkeypatch):
        path = await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        notifier = self._notifier()
        sched = self._scheduler(tmp_path, notifier)
        await sched._run_freshness_check()

        async with aiosqlite.connect(path) as conn:
            await _add_target(conn, tmp_path, "b2t", "b2", {"bucket": "b"})
        await sched._run_freshness_check()
        assert await self._activity(path, "offsite") == 1
        async with aiosqlite.connect(path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'offsite_warning_%'")
            assert (await cursor.fetchone())[0] == 0

        async with aiosqlite.connect(path) as conn:
            await conn.execute("UPDATE storage_targets SET enabled = 0 WHERE id = 'b2t'")
            await conn.commit()
        await sched._run_freshness_check()
        assert notifier.send.await_count == 2
        assert await self._activity(path, "no_offsite") == 2

    async def test_offsite_target_never_notifies(self, tmp_path, monkeypatch):
        path = await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}), ("b2t", "b2", {"bucket": "b"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        notifier = self._notifier()
        await self._scheduler(tmp_path, notifier)._run_freshness_check()
        notifier.send.assert_not_awaited()
        assert await self._activity(path, "no_offsite") == 0
        assert await self._activity(path, "offsite") == 0

    async def test_check_failure_is_contained(self, tmp_path, monkeypatch):
        await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        monkeypatch.setattr(scheduler_mod, "evaluate_offsite", AsyncMock(side_effect=RuntimeError("boom")))
        notifier = self._notifier()
        await self._scheduler(tmp_path, notifier)._run_freshness_check()
        notifier.send.assert_not_awaited()


def _healthy_freshness():
    return {"stale": False, "healthy": True, "checked_at": "2026-11-01T00:00:00Z", "stale_jobs": [], "jobs": []}
