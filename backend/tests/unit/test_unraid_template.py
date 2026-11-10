"""The Unraid Community Applications template and repository profile.

Community Apps reads ``unraid-template.xml`` and ``ca_profile.xml`` from the
default branch, and Unraid's Docker manager builds the ``docker create``
command from the template. These tests keep both files valid and keep the
template's mounts in line with Arkive's security model (data read-only,
writes only to /config, /cache and the local-target path) and with how
Unraid lays out shares (exclusive shares are symlinks in /mnt/user).
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET  # nosec B405 - parses files from this repository
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = REPO_ROOT / "unraid-template.xml"
PROFILE_PATH = REPO_ROOT / "ca_profile.xml"
RAW_PREFIX = "https://raw.githubusercontent.com/islamdiaa/Arkive/main/"

# Community Apps' category list (Squidly271/AppFeed categoryList.json).
CA_CATEGORIES = {
    "AI:",
    "Backup:",
    "Cloud:",
    "Crypto:",
    "Downloaders:",
    "Drivers:",
    "GameServers:",
    "HomeAutomation:",
    "MediaApp:Books",
    "MediaApp:Music",
    "MediaApp:Photos",
    "MediaApp:Video",
    "MediaApp:Other",
    "MediaServer:Books",
    "MediaServer:Music",
    "MediaServer:Photos",
    "MediaServer:Video",
    "MediaServer:Other",
    "Network:DNS",
    "Network:FTP",
    "Network:Management",
    "Network:Messenger",
    "Network:Proxy",
    "Network:Voip",
    "Network:VPN",
    "Network:Privacy",
    "Network:Web",
    "Network:Other",
    "Productivity:",
    "Security:",
    "Tools:System",
    "Tools:Utilities",
    "Other:",
}


def _parse(path: Path) -> ET.Element:
    return ET.parse(path).getroot()  # nosec B314 - trusted file from this repository


@pytest.fixture(scope="module")
def template() -> ET.Element:
    return _parse(TEMPLATE_PATH)


def _text(root: ET.Element, tag: str) -> str:
    element = root.find(tag)
    assert element is not None, f"<{tag}> is missing"
    return (element.text or "").strip()


def _configs(root: ET.Element, config_type: str) -> list[dict[str, str]]:
    return [dict(c.attrib) for c in root.findall("Config") if c.get("Type") == config_type]


def _repo_file_for(url: str) -> Path:
    assert url.startswith(RAW_PREFIX), f"{url} is not a raw URL on this repository's main branch"
    return REPO_ROOT / url[len(RAW_PREFIX) :]


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def _image_version() -> str:
    match = re.search(r"^ARG VERSION=(\S+)$", (REPO_ROOT / "Dockerfile").read_text(), re.MULTILINE)
    assert match, "Dockerfile has no ARG VERSION"
    return match.group(1)


class TestTemplateMetadata:
    def test_is_a_v2_container_template(self, template):
        assert template.tag == "Container"
        assert template.get("version") == "2"
        assert _text(template, "Name") == "Arkive"

    def test_links_point_at_this_repository(self, template):
        assert _text(template, "Project") == "https://github.com/islamdiaa/Arkive"
        assert _text(template, "Support").startswith("https://")
        assert _text(template, "Registry") == "https://hub.docker.com/r/islamdiaa/arkive"

    def test_template_url_is_this_file(self, template):
        assert _repo_file_for(_text(template, "TemplateURL")) == TEMPLATE_PATH

    def test_icon_is_a_square_png_in_the_repository(self, template):
        # Unraid's Docker tab shows a question mark for SVG icons.
        icon = _repo_file_for(_text(template, "Icon"))
        assert icon.is_file(), f"{icon} does not exist"
        width, height = _png_size(icon)
        assert width == height >= 128

    def test_screenshots_exist(self, template):
        screenshots = [s.text.strip() for s in template.findall("Screenshot")]
        assert screenshots
        for url in screenshots:
            assert _repo_file_for(url).is_file(), url

    def test_categories_are_valid(self, template):
        categories = _text(template, "Category").split()
        assert categories
        assert set(categories) <= CA_CATEGORIES
        assert "Backup:" in categories

    def test_repository_tag_follows_releases(self, template):
        # The release workflow publishes {{major}} for every final vX.Y.Z tag,
        # while :latest is rebuilt from main. Keep {{major}} unconditional
        # (metadata-action's docs suggest disabling it for 0.x, which would
        # freeze the template's :0).
        #
        # The template's major may lag the Dockerfile's VERSION by one: a
        # release-prep PR bumps VERSION days before the tag is built, and
        # pre-release tags don't publish {{major}}, so the new major tag doesn't
        # exist yet. It must never be ahead, or installs fail to pull.
        image, _, tag = _text(template, "Repository").partition(":")
        assert image == "islamdiaa/arkive"
        assert tag.isdigit(), f"use a major-version tag, not {tag!r}"
        version_major = int(_image_version().split(".")[0])
        assert version_major - 1 <= int(tag) <= version_major
        workflow = (REPO_ROOT / ".github" / "workflows" / "build.yml").read_text()
        assert re.search(r"^\s*type=semver,pattern=\{\{major\}\}(,enable=.*)?\s*$", workflow, re.MULTILINE)

    def test_overview_renders_cleanly_in_community_apps(self, template):
        overview = template.find("Overview").text
        assert len(overview.strip()) > 100
        # CA turns [x] into <x> tags and four spaces into indentation.
        assert "[" not in overview and "]" not in overview
        assert "<" not in overview
        assert not any(line.startswith("    ") for line in overview.splitlines())

    def test_webui_uses_the_web_ui_port(self, template):
        ports = _configs(template, "Port")
        assert [p["Target"] for p in ports] == ["8200"]
        assert _text(template, "WebUI") == "http://[IP]:[PORT:8200]"

    def test_extra_params_run_one_plain_container(self, template):
        params = _text(template, "ExtraParams")
        assert "--user 0:0" in params
        for forbidden in (";", "&&", "|", "`", "$(", "--privileged", "--cap-add", "-v ", "--volume"):
            assert forbidden not in params
        assert _text(template, "Privileged") == "false"
        assert (template.find("PostArgs").text or "").strip() == ""

    def test_no_timezone_variable(self, template):
        # Unraid passes the server's TZ to every container; a TZ entry in the
        # template comes later on the command line and would override it.
        assert "TZ" not in {v["Target"] for v in _configs(template, "Variable")}


class TestTemplateMounts:
    @pytest.fixture
    def paths(self, template) -> dict[str, dict[str, str]]:
        return {p["Target"]: p for p in _configs(template, "Path")}

    def test_data_sources_are_read_only(self, paths):
        # Discovery rewrites container mounts to /mnt/user/appdata/<app>, so
        # appdata must be at that path inside the container.
        assert paths["/mnt/user/appdata"]["Default"] == "/mnt/user/appdata"
        assert paths["/mnt/user/appdata"]["Required"] == "true"
        assert paths["/boot-config"]["Default"] == "/boot/config"
        assert paths["/var/run/docker.sock"]["Default"] == "/var/run/docker.sock"
        for target in ("/mnt/user/appdata", "/boot-config", "/var/run/docker.sock"):
            assert paths[target]["Mode"] == "ro", target

    def test_shares_are_mapped_directly_and_never_nested(self, paths):
        # With "Permit exclusive shares", /mnt/user/appdata is a symlink to
        # /mnt/cache/appdata. Mapping /mnt/user as a whole hands the container
        # that symlink, which leads nowhere inside it: appdata becomes
        # invisible to setup, the directory scan and SQLite discovery. Adding
        # a direct appdata mapping inside it doesn't help either: Docker mounts
        # it at /mnt/cache/appdata, /mnt/user/appdata stays a symlink, and
        # restic backs up the symlink instead of the data. And a read-write
        # path nested in a read-only parent stops the container from starting
        # when the inner host folder doesn't exist under the parent.
        assert "/mnt/user" not in paths
        assert "/mnt" not in paths
        for outer in paths:
            for inner in paths:
                assert not inner.startswith(outer.rstrip("/") + "/"), f"{inner} is nested inside {outer}"

    def test_only_arkive_storage_is_writable(self, paths):
        writable = {target for target, p in paths.items() if p["Mode"] != "ro"}
        assert writable == {"/config", "/cache", "/mnt/user/backups"}
        assert paths["/config"]["Default"] == "/mnt/user/appdata/arkive"
        assert paths["/config"]["Required"] == "true"

    def test_every_entry_is_complete(self, template):
        for config in template.findall("Config"):
            for attribute in ("Name", "Target", "Default", "Description", "Type", "Display", "Required", "Mask"):
                assert config.get(attribute) is not None, (config.get("Name"), attribute)
            assert config.get("Display") in ("always", "advanced")


class TestProfile:
    def test_profile_has_overview_icon_and_web_page(self):
        root = _parse(PROFILE_PATH)
        assert root.tag == "CommunityApplications"
        assert len(_text(root, "Profile")) > 50
        assert _text(root, "WebPage") == "https://github.com/islamdiaa/Arkive"
        icon = _repo_file_for(_text(root, "Icon"))
        assert icon.is_file()
        _png_size(icon)


def _use_mountinfo(monkeypatch, mountinfo: Path) -> None:
    """Point the local-target mount checks at a fake mountinfo, as if running
    in a container."""
    import app.api.targets as targets_mod
    from app.utils import mounts

    monkeypatch.setattr(targets_mod, "read_only_mount_for", lambda p: mounts.read_only_mount_for(p, mountinfo))
    monkeypatch.setattr(
        targets_mod,
        "ephemeral_path_reason",
        lambda p: mounts.ephemeral_path_reason(p, mountinfo, in_container=True),
    )


def _write_mountinfo(path: Path, mounts: list[tuple[str, str, str]]) -> Path:
    lines = ["100 90 0:40 / / rw,relatime - overlay overlay rw"]
    for i, (source, target, mode) in enumerate(mounts, start=101):
        lines.append(f"{i} 100 0:50 {source} {target} {mode},noatime - shfs shfs rw")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestLocalTargetsUnderTemplateMounts:
    """A local target must be on a persistent, read-write mount. With the
    template's mounts, that's /mnt/user/backups (or /config), not appdata and
    not an unmapped share."""

    @pytest.fixture
    def template_mounts(self, template, tmp_path, monkeypatch):
        mounts = [(p["Default"], p["Target"], p["Mode"]) for p in _configs(template, "Path")]
        _use_mountinfo(monkeypatch, _write_mountinfo(tmp_path / "mountinfo", mounts))

    @pytest.mark.parametrize("path", ["/mnt/user/backups", "/mnt/user/backups/arkive", "/config/local-repo"])
    def test_writable_paths_are_accepted(self, template_mounts, path):
        from app.api.targets import _validate_local_path

        assert _validate_local_path({"path": path}) == []

    def test_read_only_appdata_is_rejected(self, template_mounts):
        from app.api.targets import _validate_local_path

        errors = _validate_local_path({"path": "/mnt/user/appdata/repo"})
        assert len(errors) == 1
        assert "/mnt/user/appdata, which is mounted read-only" in errors[0]

    def test_unmapped_share_is_rejected_as_not_persistent(self, template_mounts):
        from app.api.targets import _validate_local_path

        errors = _validate_local_path({"path": "/mnt/user/photos/repo"})
        assert len(errors) == 1
        assert "not persistent storage" in errors[0]

    async def test_create_target_on_read_only_mount_returns_422(self, template_mounts, client):
        from tests.conftest import auth_headers, do_setup

        api_key = (await do_setup(client))["api_key"]
        resp = await client.post(
            "/api/targets",
            json={"name": "Appdata", "type": "local", "config": {"path": "/mnt/user/appdata/arkive-repo"}},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 422
        assert "mounted read-only" in resp.text


class TestLocalTargetsOnExclusiveShares:
    """An exclusive share is a symlink in /mnt/user that points at its pool.
    With /mnt/user mounted read-only (as docker-compose.yml does) plus a
    read-write mapping for such a share, Docker mounts the share at the
    symlink's target. The checks must follow the symlink, not match the path
    as typed."""

    @pytest.fixture
    def user_shares(self, tmp_path, monkeypatch) -> Path:
        base = tmp_path.resolve()
        user, pool = base / "user", base / "cache"
        (pool / "backups").mkdir(parents=True)
        user.mkdir()
        (user / "backups").symlink_to(pool / "backups")
        (user / "gone").symlink_to(pool / "gone")  # a share on a pool the container can't see
        mounts = [(str(user), str(user), "ro"), (str(pool / "backups"), str(pool / "backups"), "rw")]
        _use_mountinfo(monkeypatch, _write_mountinfo(base / "mountinfo", mounts))
        return user

    def test_writable_share_behind_a_symlink_is_accepted(self, user_shares):
        from app.api.targets import _validate_local_path

        config = {"path": str(user_shares / "backups" / "arkive")}
        assert _validate_local_path(config) == []
        assert config["path"] == str(user_shares / "backups" / "arkive")  # stored as typed

    def test_symlink_into_the_container_filesystem_is_rejected(self, user_shares):
        from app.api.targets import _validate_local_path

        errors = _validate_local_path({"path": str(user_shares / "gone" / "arkive")})
        assert len(errors) == 1
        assert "not persistent storage" in errors[0]
        assert "resolves to" in errors[0]
