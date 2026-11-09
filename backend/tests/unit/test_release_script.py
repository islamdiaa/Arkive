"""Tests for scripts/release.py, which CI uses to gate and publish releases.

The lint job runs ``release.py check`` on every push (and with ``--tag`` on a
version tag), the build job asks ``tag-info`` whether a tag may move Docker
``latest``, and the release job publishes ``notes`` as the GitHub release body.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("arkive_release_script", REPO_ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release
_spec.loader.exec_module(release)


CHANGELOG = """\
# Changelog

Intro text.

## [Unreleased]

### Added
- Something new.

## [1.2.0] - 2026-11-01

### Fixed
- A bug.

## [1.1.0] - 2026-10-01

### Added
- A feature.

[Unreleased]: https://example.test/compare/v1.2.0...HEAD
[1.2.0]: https://example.test/compare/v1.1.0...v1.2.0
[1.1.0]: https://example.test/releases/tag/v1.1.0
"""


def make_repo(root: Path, version: str = "1.2.0", changelog: str = CHANGELOG, **overrides: str) -> Path:
    files = {
        "backend/pyproject.toml": (
            f'[project]\nname = "arkive"\nversion = "{version}"\n\n[tool.ruff]\ntarget-version = "py312"\n'
        ),
        "backend/app/__init__.py": f'import os\n\n__version__ = os.environ.get("ARKIVE_VERSION") or "{version}"\n',
        "frontend/package.json": json.dumps({"name": "arkive-frontend", "version": version}),
        "frontend/package-lock.json": json.dumps(
            {
                "name": "arkive-frontend",
                "version": version,
                "lockfileVersion": 3,
                "packages": {"": {"name": "arkive-frontend", "version": version}},
            }
        ),
        "Dockerfile": f"FROM python:3.12-slim AS runtime\nARG TARGETARCH\nARG VERSION={version}\n",
        "CHANGELOG.md": changelog,
    }
    files.update(overrides)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Tags and versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.2.3", ("1.2.3", False)),
        ("v0.4.0", ("0.4.0", False)),
        ("v1.2.3-rc.1", ("1.2.3-rc.1", True)),
        ("v10.0.0-beta", ("10.0.0-beta", True)),
    ],
)
def test_parse_tag(tag, expected):
    assert release.parse_tag(tag) == expected


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v01.2.3", "vfoo", "v1.2.3 ", "v1.2.3+build", "v1.2.3-"])
def test_parse_tag_rejects_non_version_tags(tag):
    with pytest.raises(release.ReleaseError):
        release.parse_tag(tag)


def test_version_key_follows_semver_precedence():
    ordered = [
        "0.9.9",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-beta",
        "1.0.0-rc.2",
        "1.0.0-rc.10",
        "1.0.0",
        "1.0.1",
        "1.10.0",
    ]
    assert sorted(reversed(ordered), key=release.version_key) == ordered


@pytest.mark.parametrize(
    "tag,existing,latest,newest_in_major",
    [
        # A new stable release moves both.
        ("v0.5.0", ["v0.2.4", "v0.4.0"], "true", "true"),
        # The tag itself may or may not already be in the listing.
        ("v0.5.0", ["v0.4.0", "v0.5.0"], "true", "true"),
        # A fix on an older line moves neither: latest and "0" stay on 0.5.0.
        ("v0.4.1", ["v0.4.0", "v0.5.0"], "false", "false"),
        # A fix on an older major moves its own major tag but not latest.
        ("v1.4.1", ["v1.4.0", "v2.0.0"], "false", "true"),
        # Pre-release tags and other tags don't count as newer releases.
        ("v0.4.2", ["v0.4.1", "v0.5.0-rc.1", "nightly", "v9-broken"], "true", "true"),
    ],
)
def test_tag_info_moves_latest_only_for_the_newest_stable_release(tag, existing, latest, newest_in_major):
    info = release.tag_info(tag, existing)
    assert info["latest"] == latest
    assert info["newest_in_major"] == newest_in_major
    assert info["prerelease"] == "false"
    assert info["version"] == tag[1:]


def test_tag_info_never_moves_latest_for_a_prerelease():
    info = release.tag_info("v2.0.0-rc.1", ["v1.0.0"])
    assert info == {"version": "2.0.0-rc.1", "prerelease": "true", "latest": "false", "newest_in_major": "false"}


def test_tag_info_rejects_a_non_version_tag():
    with pytest.raises(release.ReleaseError):
        release.tag_info("vnext", ["v1.0.0"])


def test_read_tag_list_accepts_ls_remote_output_and_plain_lines():
    text = (
        "1c2c915b8cc44ea5dd36a5f5d0f8db20585238ce\trefs/tags/v0.4.0\n"
        "9933cc37a9526507e686b7ae92a1c613dab8752b\trefs/tags/v0.2.0^{}\n"
        "\n"
        "v0.1.0\n"
    )
    assert release.read_tag_list(text) == ["v0.4.0", "v0.2.0", "v0.1.0"]


# ---------------------------------------------------------------------------
# CHANGELOG.md
# ---------------------------------------------------------------------------


def test_release_notes_are_the_version_section_plus_its_link():
    notes = release.release_notes(release.parse_changelog(CHANGELOG), "v1.2.0")
    assert notes == "### Fixed\n- A bug.\n\n**Full changelog:** https://example.test/compare/v1.1.0...v1.2.0\n"


def test_release_notes_for_the_oldest_section_leave_out_the_link_block():
    notes = release.release_notes(release.parse_changelog(CHANGELOG), "v1.1.0")
    assert "[1.2.0]:" not in notes
    assert notes.startswith("### Added\n- A feature.\n\n**Full changelog:** https://example.test/releases/tag/v1.1.0")


def test_release_notes_for_a_prerelease_fall_back_to_unreleased():
    notes = release.release_notes(release.parse_changelog(CHANGELOG), "v1.3.0-rc.1")
    assert notes == "### Added\n- Something new.\n"


def test_release_notes_require_a_section_for_a_stable_tag():
    with pytest.raises(release.ReleaseError, match=r"no section for 1\.3\.0"):
        release.release_notes(release.parse_changelog(CHANGELOG), "v1.3.0")


def test_release_notes_refuse_an_empty_section():
    log = release.parse_changelog(CHANGELOG.replace("### Added\n- Something new.\n", ""))
    with pytest.raises(release.ReleaseError, match="empty"):
        release.release_notes(log, "v1.3.0-rc.1")


def test_valid_changelog_has_no_errors():
    assert release.validate_changelog(release.parse_changelog(CHANGELOG)) == []


def test_malformed_version_heading_is_an_error():
    with pytest.raises(release.ReleaseError, match="CHANGELOG.md:10:"):
        release.parse_changelog(CHANGELOG.replace("## [1.2.0] - 2026-11-01", "## 1.2.0 (2026-11-01)"))


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("## [Unreleased]\n", "", "first section must be '## [Unreleased]'"),
        ("## [1.1.0] - 2026-10-01", "## [1.3.0] - 2026-10-01", "must come after [1.2.0]"),
        ("## [1.1.0] - 2026-10-01", "## [1.2.0] - 2026-10-01", "duplicate section [1.2.0]"),
        ("[1.1.0]: https://example.test/releases/tag/v1.1.0\n", "", "no link definition '[1.1.0]: <url>'"),
        ("## [1.2.0] - 2026-11-01", "## [1.2.0]", "needs a release date"),
        ("## [1.2.0] - 2026-11-01", "## [1.2.0] - 2026-13-01", "not a valid date"),
        ("### Fixed\n- A bug.\n", "", "[1.2.0] is empty"),
        ("## [1.2.0] - 2026-11-01", "## [next] - 2026-11-01", "[next] is not a semantic version"),
        ("## [Unreleased]", "## [Unreleased] - 2026-11-02", "[Unreleased] takes no date"),
    ],
)
def test_changelog_validation_errors(old, new, message):
    assert old in CHANGELOG
    errors = release.validate_changelog(release.parse_changelog(CHANGELOG.replace(old, new)))
    assert any(message in e for e in errors), errors


# ---------------------------------------------------------------------------
# check(): CHANGELOG.md and version files together
# ---------------------------------------------------------------------------


def test_check_passes_for_a_consistent_repo(tmp_path):
    root = make_repo(tmp_path)
    assert release.check(root) == []
    assert release.check(root, "v1.2.0") == []


def test_check_reports_version_files_that_disagree(tmp_path):
    root = make_repo(tmp_path, **{"Dockerfile": "FROM scratch\nARG VERSION=1.1.0\n"})
    errors = release.check(root)
    assert len(errors) == 1
    assert "version files disagree" in errors[0]
    assert "Dockerfile=1.1.0" in errors[0]


def test_check_requires_a_section_for_the_current_version(tmp_path):
    root = make_repo(tmp_path, version="1.3.0")
    assert release.check(root) == ["version files say 1.3.0 but CHANGELOG.md has no [1.3.0] section"]


def test_check_with_a_tag_that_does_not_match_the_version_files(tmp_path):
    root = make_repo(tmp_path)
    errors = release.check(root, "v1.1.0")
    assert len(errors) == 1
    assert "tag v1.1.0 does not match the version files" in errors[0]


def test_check_with_a_tag_that_has_no_section_reports_both_problems(tmp_path):
    root = make_repo(tmp_path)
    errors = release.check(root, "v1.3.0")
    assert len(errors) == 2
    assert "no section for 1.3.0" in errors[0]
    assert "tag v1.3.0 does not match the version files" in errors[1]


def test_check_with_a_prerelease_tag_skips_the_version_comparison(tmp_path):
    root = make_repo(tmp_path)
    assert release.check(root, "v1.3.0-rc.1") == []


def test_check_rejects_a_tag_that_is_not_a_version(tmp_path):
    root = make_repo(tmp_path)
    errors = release.check(root, "vnext")
    assert any("not a version tag" in e for e in errors)


def test_check_reports_a_missing_version_file(tmp_path):
    root = make_repo(tmp_path)
    (root / "frontend" / "package-lock.json").unlink()
    errors = release.check(root)
    assert any(e.startswith("frontend/package-lock.json: cannot read") for e in errors)


def test_check_reports_a_missing_changelog(tmp_path):
    root = make_repo(tmp_path)
    (root / "CHANGELOG.md").unlink()
    errors = release.check(root)
    assert len(errors) == 1
    assert errors[0].startswith("CHANGELOG.md: cannot read")


# ---------------------------------------------------------------------------
# CLI, as the workflow calls it
# ---------------------------------------------------------------------------


def test_cli_check_exit_codes(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert release.main(["--root", str(root), "check", "--tag", ""]) == 0
    assert release.main(["--root", str(root), "check", "--tag", "v1.2.0"]) == 0
    assert release.main(["--root", str(root), "check", "--tag", "v9.9.9"]) == 1
    assert "no section for 9.9.9" in capsys.readouterr().err


def test_cli_notes_prints_the_section(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert release.main(["--root", str(root), "notes", "v1.2.0"]) == 0
    assert capsys.readouterr().out.startswith("### Fixed\n- A bug.\n")
    assert release.main(["--root", str(root), "notes", "v7.0.0"]) == 1


def test_cli_tag_info_prints_github_output_lines(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("abc\trefs/tags/v0.4.0\ndef\trefs/tags/v0.5.0\n"))
    assert release.main(["tag-info", "v0.4.1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "version=0.4.1",
        "prerelease=false",
        "latest=false",
        "newest_in_major=false",
    ]


def test_cli_errors_use_github_annotations_in_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    root = make_repo(tmp_path)
    assert release.main(["--root", str(root), "notes", "v7.0.0"]) == 1
    assert capsys.readouterr().err.startswith("::error::")


# ---------------------------------------------------------------------------
# This repository
# ---------------------------------------------------------------------------


def test_repository_changelog_and_version_files_are_consistent():
    assert release.check(REPO_ROOT) == []


def test_repository_has_release_notes_for_the_current_version():
    current = dict(release.read_versions(REPO_ROOT))["backend/pyproject.toml"]
    notes = release.release_notes(release.load_changelog(REPO_ROOT), f"v{current}")
    assert notes.strip()
