"""3-2-1 awareness: detecting when every target the backup jobs upload to is
on this server, reporting it in /api/status, and telling the user once."""

import ipaddress
import json
import socket
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
NETWORK_CFG_DHCP = 'USE_DHCP[0]="yes"\n'

MOUNTINFO = """\
100 90 0:40 / / rw,relatime - overlay overlay rw
102 100 0:50 /appdata/arkive /config rw,noatime - shfs shfs rw
104 100 0:50 /backups /mnt/user/backups rw,noatime - shfs shfs rw
105 100 0:60 / /mnt/remotes/nas rw,relatime - cifs //nas/backup rw
106 100 0:61 / /mnt/disks/usb rw,relatime - xfs /dev/sdx1 rw
"""


def _ip(text: str):
    return ipaddress.ip_address(text)


@pytest.fixture
def dns():
    """Name -> addresses answered by the fake resolver; anything else fails."""
    return {}


@pytest.fixture(autouse=True)
def _isolated_identity(tmp_path, monkeypatch, dns):
    """Point the identity readers at fixtures instead of this machine."""
    proc_net = tmp_path / "proc_net"
    proc_net.mkdir()
    (proc_net / "fib_trie").write_text(FIB_TRIE)
    (proc_net / "if_inet6").write_text(IF_INET6)
    boot = tmp_path / "boot-config"
    boot.mkdir()
    (boot / "network.cfg").write_text(NETWORK_CFG)
    (boot / "ident.cfg").write_text('NAME="tower"\n')

    def getaddrinfo(host, port, *args, **kwargs):
        answers = dns.get(host)
        if isinstance(answers, Exception):
            raise answers
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in answers]

    monkeypatch.setattr(offsite, "PROC_NET", proc_net)
    monkeypatch.setattr(offsite, "BOOT_CONFIG", boot)
    monkeypatch.setattr(offsite.socket, "gethostname", lambda: "0123456789ab")
    monkeypatch.setattr(offsite.socket, "getaddrinfo", getaddrinfo)
    offsite.reset_identity_cache()
    yield
    offsite.reset_identity_cache()
    _reset_fernet()


def _network(driver: str, gateway: str | None, *, aux: dict | None = None, containers: dict | None = None):
    config = [{"Subnet": "10.0.0.0/24", "Gateway": gateway, "AuxiliaryAddresses": aux}] if gateway else []
    endpoints = {
        f"{i:064x}": {"Name": name, "IPv4Address": f"{address}/24", "IPv6Address": ""}
        for i, (name, address) in enumerate((containers or {}).items())
    }
    return SimpleNamespace(attrs={"Driver": driver, "IPAM": {"Config": config}, "Containers": endpoints})


def _docker(*networks, name="tower"):
    client = MagicMock()
    client.info.return_value = {"Name": name}
    client.networks.list.return_value = list(networks)
    return client


def _failing_docker():
    client = MagicMock()
    client.info.side_effect = RuntimeError("daemon down")
    client.networks.list.side_effect = RuntimeError("daemon down")
    return client


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
    containers={_ip("172.20.0.3"): "minio", _ip("192.168.1.50"): "garage", _ip("172.20.0.9"): "vaultwarden-backup-1"},
    resolved={
        "minio": frozenset({_ip("172.20.0.3")}),
        "tower.example.com": frozenset({_ip("203.0.113.10")}),
        "nas": frozenset({_ip("192.168.1.60")}),
        "backup.lan": frozenset({_ip("192.168.1.61")}),
        "s3.amazonaws.com": frozenset({_ip("203.0.113.20")}),
    },
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
        ("192.168.1.50", True),  # a container's own LAN address (macvlan/ipvlan)
        ("192.168.1.51", False),
        ("minio", True),  # Docker's DNS answered with a container's address
        ("nas", False),
        ("backup.lan", False),
        ("s3.amazonaws.com", False),
        ("unresolved", False),
        ("", False),
    ],
)
def test_host_locality(host, local):
    assert (offsite.host_locality(host, IDENTITY) is not None) is local


def test_host_locality_explains_resolved_containers():
    assert (
        offsite.host_locality("minio", IDENTITY) == "minio resolves to 172.20.0.3, the container minio on this server"
    )
    assert offsite.host_locality("192.168.1.50", IDENTITY) == "192.168.1.50 is the container garage on this server"


def test_container_names_alone_never_make_a_host_local():
    """A LAN host named like a container (or a compose service) is another machine."""
    identity = offsite.LocalIdentity(
        containers={_ip("172.20.0.9"): "vaultwarden-backup-1", _ip("172.20.0.10"): "nas", _ip("172.20.0.11"): "db"},
        resolved={
            "backup.lan": frozenset({_ip("192.168.1.61")}),
            "nas.local": frozenset({_ip("192.168.1.62")}),
            "db.internal": frozenset({_ip("192.168.1.63")}),
            "nas": frozenset({_ip("192.168.1.62")}),
        },
    )
    for host in ("backup.lan", "nas.local", "db.internal", "nas", "backup"):
        assert offsite.host_locality(host, identity) is None, host


def test_host_is_placed_only_with_complete_information():
    assert offsite.host_is_placed("192.168.1.51", IDENTITY)
    assert offsite.host_is_placed("NAS", IDENTITY)
    assert not offsite.host_is_placed("unresolved", IDENTITY)
    incomplete = offsite.LocalIdentity(resolved={"nas": frozenset({_ip("192.168.1.60")})}, complete=False)
    assert not offsite.host_is_placed("nas", incomplete)
    assert not offsite.host_is_placed("192.168.1.51", incomplete)


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


def test_docker_identity_reads_gateways_aux_addresses_and_containers():
    client = _docker(
        _network("bridge", "172.17.0.1", containers={"minio": "172.17.0.3"}),
        _network("bridge", "172.20.0.1"),
        # Unraid's br0: the gateway is the LAN router, the "server" aux address is this host.
        _network("ipvlan", "192.168.1.1", aux={"server": "192.168.1.100"}, containers={"garage": "192.168.1.50"}),
        _network("macvlan", "192.168.2.1", aux={"other": "192.168.2.9"}),
        _network("host", None),
        name="Tower",
    )
    names, addresses, containers, complete = offsite._docker_identity(client)
    assert complete is True
    assert names == {"tower"}
    assert addresses == {_ip("172.17.0.1"), _ip("172.20.0.1"), _ip("192.168.1.100")}
    assert containers == {_ip("172.17.0.3"): "minio", _ip("192.168.1.50"): "garage"}
    client.networks.list.assert_called_with(greedy=True)


def test_docker_identity_reports_daemon_errors_as_incomplete():
    assert offsite._docker_identity(_failing_docker()) == (set(), set(), {}, False)
    # No Docker socket at all: nothing to ask, which is not a failure.
    assert offsite._docker_identity(None) == (set(), set(), {}, True)


def test_gather_keeps_last_docker_answer_through_a_hiccup():
    first = offsite.gather_system_identity(_docker(_network("bridge", "172.17.0.1"), name="test-server"))
    assert first.complete and _ip("172.17.0.1") in first.addresses and "test-server" in first.names

    during = offsite.gather_system_identity(_failing_docker())
    assert during.complete
    assert _ip("172.17.0.1") in during.addresses and "test-server" in during.names

    # A fresh process that never reached Docker can't vouch for anything.
    offsite.reset_identity_cache()
    cold = offsite.gather_system_identity(_failing_docker())
    assert cold.complete is False and _ip("172.17.0.1") not in cold.addresses


def test_gather_finds_a_dhcp_address_through_the_servers_own_name(dns):
    (offsite.BOOT_CONFIG / "network.cfg").write_text(NETWORK_CFG_DHCP)
    dns["tower"] = ["192.168.1.100"]
    dns["test-server"] = ["8.8.8.8"]  # a search-domain answer on the internet is not this server
    identity = offsite.gather_system_identity(_docker(name="test-server"))
    assert _ip("192.168.1.100") in identity.addresses
    assert _ip("8.8.8.8") not in identity.addresses


def test_gather_resolves_target_hosts_and_remembers_answers(dns):
    dns["minio"] = ["172.20.0.3"]
    identity = offsite.gather_system_identity(None, {"minio", "gone"})
    assert identity.resolved == {"minio": frozenset({_ip("172.20.0.3")})}

    dns["minio"] = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    again = offsite.gather_system_identity(None, {"minio"})
    assert again.resolved == {"minio": frozenset({_ip("172.20.0.3")})}


def test_system_identity_is_cached_and_never_stacks_gathers(monkeypatch):
    calls = []

    def gather(docker_client=None, hosts=()):
        calls.append(frozenset(hosts))
        return offsite.LocalIdentity(names=frozenset({"tower"}))

    monkeypatch.setattr(offsite, "gather_system_identity", gather)
    first = offsite.cached_system_identity()
    assert offsite.cached_system_identity() is first
    assert len(calls) == 1
    # A host that wasn't looked up yet needs a new gather.
    offsite.cached_system_identity(None, {"minio"})
    assert calls[-1] == {"minio"} and len(calls) == 2
    offsite.cached_system_identity(None, set())
    assert len(calls) == 2

    # While a gather is in flight (e.g. stuck on Docker), callers get the
    # last result instead of starting another one.
    offsite.reset_identity_cache()
    lock: threading.Lock = offsite._identity_lock
    with lock:
        assert offsite.cached_system_identity() is None
    assert len(calls) == 2


def test_incomplete_identity_is_retried_sooner(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(offsite.time, "monotonic", lambda: clock[0])
    calls = []

    def gather(docker_client=None, hosts=()):
        calls.append(1)
        return offsite.LocalIdentity(complete=len(calls) > 1)

    monkeypatch.setattr(offsite, "gather_system_identity", gather)
    assert offsite.cached_system_identity().complete is False
    clock[0] += offsite.INCOMPLETE_IDENTITY_TTL_SECONDS + 1
    assert offsite.cached_system_identity().complete is True
    clock[0] += offsite.INCOMPLETE_IDENTITY_TTL_SECONDS + 1
    offsite.cached_system_identity()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# evaluate_offsite
# ---------------------------------------------------------------------------


async def _add_job(db, job_id, targets=(), enabled=1, name=None):
    await db.execute(
        "INSERT INTO backup_jobs (id, name, type, schedule, enabled, targets) VALUES (?, ?, 'full', '0 2 * * *', ?, ?)",
        (job_id, name or f"Job {job_id}", enabled, json.dumps(list(targets))),
    )
    await db.commit()


@pytest.fixture
async def db(tmp_path):
    """A database with one enabled job that uploads to every enabled target."""
    path = tmp_path / "arkive.db"
    await init_db(path)
    await run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        await _add_job(conn, "all", name="Nightly")
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
        "uncertain": False,
        "enabled_targets": 0,
        "offsite_targets": 0,
        "local_targets": [],
        "unused_offsite_targets": [],
        "message": None,
    }


async def test_only_local_targets_warn(db, tmp_path):
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    await _add_target(db, tmp_path, "share", "local", {"path": "/mnt/user/backups"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True and result["has_offsite_copy"] is False and result["uncertain"] is False
    assert result["enabled_targets"] == 2 and result["offsite_targets"] == 0
    assert {t["id"] for t in result["local_targets"]} == {"usb", "share"}
    assert all(t["type"] == "local" and "on this server" in t["reason"] for t in result["local_targets"])
    assert "no offsite copy" in result["message"] and "Add a target" in result["message"]


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
    assert result["unused_offsite_targets"] == []


async def test_local_target_on_network_share_is_offsite(db, tmp_path):
    await _add_target(db, tmp_path, "nas", "local", {"path": "/mnt/remotes/nas/arkive"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is False and result["has_offsite_copy"] is True


async def test_offsite_target_no_job_uploads_to_is_not_an_offsite_copy(db, tmp_path):
    """Setup pins Full Backup to the target chosen then; a target added later
    is not one it uploads to, so it must not clear the warning."""
    await db.execute("UPDATE backup_jobs SET enabled = 0 WHERE id = 'all'")
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    await _add_job(db, "full", ["usb"], name="Full Backup")
    await _add_job(db, "flash", [], enabled=0, name="Flash Backup")
    await _add_target(db, tmp_path, "b2t", "b2", {"bucket": "b"})

    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True and result["has_offsite_copy"] is False
    assert result["enabled_targets"] == 2 and result["offsite_targets"] == 0
    assert [t["id"] for t in result["local_targets"]] == ["usb"]
    assert result["unused_offsite_targets"] == [{"id": "b2t", "name": "Target b2t", "type": "b2"}]
    assert (
        "Target b2t is enabled, but no enabled backup job uploads to it: "
        "Full Backup uploads only to the targets chosen for it." in result["message"]
    )

    # A job that uploads to every enabled target takes the backups offsite.
    await db.execute("UPDATE backup_jobs SET enabled = 1 WHERE id = 'flash'")
    await db.commit()
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is False and result["has_offsite_copy"] is True
    assert result["unused_offsite_targets"] == []


async def test_job_pinned_to_an_offsite_target(db, tmp_path):
    await db.execute("DELETE FROM backup_jobs")
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    await _add_target(db, tmp_path, "b2t", "b2", {"bucket": "b"}, enabled=0)
    # Like the orchestrator, a target named on the job is used even when disabled.
    await _add_job(db, "full", ["b2t", "missing"])
    result = await _evaluate(db, tmp_path)
    assert result["has_offsite_copy"] is True and result["warning"] is False
    assert result["local_targets"] == []  # usb is enabled but not used


async def test_no_enabled_job_is_not_a_warning(db, tmp_path):
    await db.execute("UPDATE backup_jobs SET enabled = 0")
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is False and result["has_offsite_copy"] is False
    assert result["enabled_targets"] == 1 and result["local_targets"] == []


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
async def test_self_hosted_targets_pointing_back_here(db, tmp_path, dns, target_type, config, local):
    dns["backup.example.com"] = ["203.0.113.10"]
    dns["s3.eu-central-1.amazonaws.com"] = ["203.0.113.20"]
    await _add_target(db, tmp_path, "t1", target_type, config)
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is local
    assert result["has_offsite_copy"] is not local
    assert result["uncertain"] is False


async def test_s3_on_a_container_reached_by_name_or_lan_address(db, tmp_path, dns):
    dns["minio"] = ["172.20.0.3"]  # Docker's DNS on a network Arkive shares
    client = _docker(
        _network("bridge", "172.20.0.1", containers={"minio": "172.20.0.3"}),
        _network("ipvlan", "192.168.1.1", containers={"garage": "192.168.1.50"}),
    )
    await _add_target(db, tmp_path, "m1", "s3", {"endpoint": "http://minio:9000", "bucket": "b"})
    await _add_target(db, tmp_path, "g1", "s3", {"endpoint": "http://192.168.1.50:3900", "bucket": "b"})
    result = await _evaluate(db, tmp_path, docker_client=client)
    assert result["warning"] is True
    reasons = {t["id"]: t["reason"] for t in result["local_targets"]}
    assert reasons == {
        "m1": "minio resolves to 172.20.0.3, the container minio on this server",
        "g1": "192.168.1.50 is the container garage on this server",
    }


async def test_lan_host_named_like_a_compose_service_is_offsite(db, tmp_path, dns):
    """vaultwarden-backup-1 runs here; backup.lan is a different machine."""
    await db.execute(
        "INSERT INTO discovered_containers (name, image, compose_project) "
        "VALUES ('vaultwarden-backup-1', 'img', 'vaultwarden'), ('nas', 'img', NULL)"
    )
    await db.commit()
    dns["backup.lan"] = ["192.168.1.61"]
    dns["nas.local"] = ["192.168.1.62"]
    client = _docker(_network("bridge", "172.17.0.1", containers={"vaultwarden-backup-1": "172.17.0.9"}))
    await _add_target(db, tmp_path, "pi", "sftp", {"host": "backup.lan", "username": "u"})
    await _add_target(db, tmp_path, "nas", "sftp", {"host": "nas.local", "username": "u"})
    result = await _evaluate(db, tmp_path, docker_client=client)
    assert result["warning"] is False and result["has_offsite_copy"] is True
    assert result["local_targets"] == [] and result["offsite_targets"] == 2


async def test_dhcp_server_address_is_recognised(db, tmp_path, dns):
    """Unraid on DHCP: the address is in no config file. Its own name, or the
    aux address Unraid reserves on br0, gives it away."""
    (offsite.BOOT_CONFIG / "network.cfg").write_text(NETWORK_CFG_DHCP)
    await _add_target(db, tmp_path, "s3", "s3", {"endpoint": "http://192.168.1.100:9000", "bucket": "b"})

    assert (await _evaluate(db, tmp_path))["warning"] is False  # nothing to go on

    offsite.reset_identity_cache()
    dns["tower"] = ["192.168.1.100"]
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True
    assert result["local_targets"][0]["reason"] == "192.168.1.100 is an address of this server"

    offsite.reset_identity_cache()
    dns.clear()
    client = _docker(_network("ipvlan", "192.168.1.1", aux={"server": "192.168.1.100"}))
    assert (await _evaluate(db, tmp_path, docker_client=client))["warning"] is True


async def test_server_name_and_docker_bridge_gateway(db, tmp_path):
    await db.execute("INSERT INTO settings (key, value) VALUES ('server_name', 'unraid-test')")
    await db.commit()
    client = _docker(_network("bridge", "172.17.0.1"), name="test-server")
    await _add_target(db, tmp_path, "a", "sftp", {"host": "unraid-test", "username": "u"})
    await _add_target(db, tmp_path, "b", "sftp", {"host": "172.17.0.1", "username": "u"})
    await _add_target(db, tmp_path, "c", "sftp", {"host": "test-server.lan", "username": "u"})
    result = await _evaluate(db, tmp_path, docker_client=client)
    assert result["warning"] is True and len(result["local_targets"]) == 3


async def test_unreadable_remote_config_is_uncertain(db, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    await _add_target(db, other, "s1", "sftp", {"host": "localhost", "username": "u"})
    await _add_target(db, other, "l1", "local", {"path": "/mnt/remotes/nas"})
    (tmp_path / ".keyfile").unlink(missing_ok=True)
    result = await _evaluate(db, tmp_path)
    # The sftp host can't be read, so it is neither local nor evidently
    # offsite; the local target is local by type even without its path.
    assert [t["id"] for t in result["local_targets"]] == ["l1"]
    assert result["offsite_targets"] == 1
    assert result["uncertain"] is True and result["has_offsite_copy"] is False and result["warning"] is False
    assert not (tmp_path / ".keyfile").exists(), "reading configs must never create a keyfile"


async def test_uncertain_result_keeps_an_open_warning(db, tmp_path):
    await _add_target(db, tmp_path, "usb", "local", {"path": "/mnt/disks/usb"})
    await _add_target(db, tmp_path, "s1", "sftp", {"host": "unresolvable", "username": "u"})
    result = await _evaluate(db, tmp_path)
    assert result["uncertain"] is True and result["warning"] is False

    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, '2026-11-01T00:00:00Z')", (offsite.OFFSITE_WARNING_SINCE_KEY,)
    )
    await db.commit()
    result = await _evaluate(db, tmp_path)
    assert result["uncertain"] is True and result["warning"] is True and result["has_offsite_copy"] is False
    assert result["message"]


async def test_unreadable_database_is_uncertain_not_empty(tmp_path):
    async with aiosqlite.connect(tmp_path / "bare.db") as conn:
        result = await offsite.evaluate_offsite(conn)
    assert result["uncertain"] is True and result["warning"] is False and result["has_offsite_copy"] is False


async def test_plain_json_legacy_config(db, tmp_path):
    await _add_target(db, tmp_path, "s1", "sftp", {"host": "127.0.0.1", "username": "u"}, encrypted=False)
    result = await _evaluate(db, tmp_path)
    assert result["warning"] is True


async def test_slow_identity_does_not_stall(db, tmp_path, monkeypatch):
    def hang(docker_client=None, hosts=()):
        threading.Event().wait(2)
        return None

    monkeypatch.setattr(offsite, "cached_system_identity", hang)
    monkeypatch.setattr(offsite, "IDENTITY_TIMEOUT_SECONDS", 0.05)
    await _add_target(db, tmp_path, "s1", "sftp", {"host": "localhost", "username": "u"})
    await _add_target(db, tmp_path, "s2", "sftp", {"host": "192.168.1.100", "username": "u"})
    result = await _evaluate(db, tmp_path)
    # Loopback is still recognised; the host address needs the identity, and
    # without it that target is unplaced rather than offsite.
    assert [t["id"] for t in result["local_targets"]] == ["s1"]
    assert result["uncertain"] is True and result["has_offsite_copy"] is False


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
    assert before["offsite"]["uncertain"] is False
    assert before["offsite"]["local_targets"][0]["id"] == "usb"
    assert before["offsite"]["unused_offsite_targets"] == []
    assert before["offsite"]["message"]

    await _insert_target(client, "b2t", "b2", {"bucket": "b", "key_id": "k", "app_key": "s"})
    after = (await client.get("/api/status", headers=headers)).json()
    assert after["offsite"]["warning"] is False and after["offsite"]["has_offsite_copy"] is True
    # The warning never changes the overall verdict.
    assert before["status"] == after["status"]


async def test_status_still_warns_when_the_only_job_skips_the_new_target(client):
    """The reviewer's scenario: setup pinned Full Backup to a local target,
    the other jobs are off, and a B2 target is added afterwards."""
    data = await do_setup(client)
    headers = auth_headers(data["api_key"])
    async with aiosqlite.connect(_app(client).state.config.db_path) as conn:
        await conn.execute("DELETE FROM storage_targets")
        await conn.execute("UPDATE backup_jobs SET targets = '[\"usb\"]' WHERE type = 'full'")
        await conn.execute("UPDATE backup_jobs SET enabled = 0 WHERE type != 'full'")
        await conn.commit()
    await _insert_target(client, "usb", "local", {"path": "/mnt/disks/usb"})
    await _insert_target(client, "b2t", "b2", {"bucket": "b", "key_id": "k", "app_key": "s"})

    offsite_status = (await client.get("/api/status", headers=headers)).json()["offsite"]
    assert offsite_status["warning"] is True and offsite_status["has_offsite_copy"] is False
    assert [t["id"] for t in offsite_status["unused_offsite_targets"]] == ["b2t"]


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


async def test_migration_subscribes_existing_failure_channels(tmp_path):
    """Channels created before the event existed hear about it if they asked for failures."""
    path = tmp_path / "arkive.db"
    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "INSERT INTO notification_channels (id, type, name, events) VALUES "
            "('a', 'discord', 'A', '[\"backup.failed\", \"backup.stale\"]'), "
            "('b', 'ntfy', 'B', '[\"backup.success\"]'), "
            "('c', 'ntfy', 'C', '[\"backup.failed\", \"backup.no_offsite\"]')"
        )
        await conn.commit()
    await run_migrations(path)
    async with aiosqlite.connect(path) as conn:
        rows = {
            k: json.loads(v)
            for k, v in await (await conn.execute("SELECT id, events FROM notification_channels")).fetchall()
        }
    assert rows["a"].count("backup.no_offsite") == 1
    assert "backup.no_offsite" not in rows["b"]
    assert rows["c"].count("backup.no_offsite") == 1


class TestOffsiteNotification:
    async def _seed(self, tmp_path, *targets):
        path = tmp_path / "arkive.db"
        await init_db(path)
        await run_migrations(path)
        async with aiosqlite.connect(path) as conn:
            await _add_job(conn, "all")
            for target_id, target_type, config in targets:
                await _add_target(conn, tmp_path, target_id, target_type, config)
        return path

    def _scheduler(self, tmp_path, notifier, docker_client=None):
        config = SimpleNamespace(db_path=tmp_path / "arkive.db", config_dir=tmp_path, dump_dir=tmp_path / "dumps")
        discovery = SimpleNamespace(docker=docker_client) if docker_client is not None else None
        return scheduler_mod.ArkiveScheduler(
            orchestrator=MagicMock(), config=config, discovery=discovery, notifier=notifier
        )

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

    @staticmethod
    async def _markers(path):
        async with aiosqlite.connect(path) as conn:
            cursor = await conn.execute("SELECT key FROM settings WHERE key LIKE 'offsite_warning_%'")
            return {row[0] for row in await cursor.fetchall()}

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
        assert await self._markers(path) == set()

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

    async def test_docker_hiccup_neither_resets_nor_repeats_the_notice(self, tmp_path, monkeypatch):
        """MinIO reached through the docker0 gateway: only Docker knows that
        address is this server. Docker being briefly unreachable right after a
        restart must not read as "offsite now", then "local again"."""
        path = await self._seed(tmp_path, ("minio", "s3", {"endpoint": "http://172.17.0.1:9000", "bucket": "b"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        notifier = self._notifier()
        healthy = _docker(_network("bridge", "172.17.0.1"))

        await self._scheduler(tmp_path, notifier, healthy)._run_freshness_check()
        assert notifier.send.await_count == 1
        assert await self._markers(path) == {offsite.OFFSITE_WARNING_SINCE_KEY, offsite.OFFSITE_NOTIFIED_AT_KEY}

        offsite.reset_identity_cache()  # a restart: nothing remembered in memory
        await self._scheduler(tmp_path, notifier, _failing_docker())._run_freshness_check()
        assert await self._markers(path) == {offsite.OFFSITE_WARNING_SINCE_KEY, offsite.OFFSITE_NOTIFIED_AT_KEY}
        assert await self._activity(path, "offsite") == 0

        offsite.reset_identity_cache()  # Docker is back
        await self._scheduler(tmp_path, notifier, healthy)._run_freshness_check()
        assert notifier.send.await_count == 1
        assert await self._activity(path, "no_offsite") == 1

    async def test_check_failure_is_contained(self, tmp_path, monkeypatch):
        await self._seed(tmp_path, ("usb", "local", {"path": "/mnt/disks/usb"}))
        monkeypatch.setattr(scheduler_mod, "ping_heartbeat", AsyncMock())
        monkeypatch.setattr(scheduler_mod, "evaluate_offsite", AsyncMock(side_effect=RuntimeError("boom")))
        notifier = self._notifier()
        await self._scheduler(tmp_path, notifier)._run_freshness_check()
        notifier.send.assert_not_awaited()


def _healthy_freshness():
    return {"stale": False, "healthy": True, "checked_at": "2026-11-01T00:00:00Z", "stale_jobs": [], "jobs": []}
