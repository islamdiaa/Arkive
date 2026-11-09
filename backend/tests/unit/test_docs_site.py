"""The user guide (docs/, mkdocs.yml) stays buildable and in step with the code.

The docs workflow runs ``mkdocs build --strict`` only on main, so these checks
catch problems on every pull request: every page is in the nav, relative links
and anchors resolve, the API paths the guide mentions exist, the tables that
list providers, notification events and defaults match the code, and the
deploy workflow publishes from main only.
"""

import re
import unicodedata
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
DOCS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"
README = REPO_ROOT / "README.md"

pytestmark = pytest.mark.skipif(not MKDOCS_YML.is_file(), reason="repository docs are not available")

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
# Arkive API paths: bare (`/api/status`) or on localhost, not other sites' /api URLs.
_API_PATH_RE = re.compile(r"""(?:(?<=[\s`("'])|^|(?<=localhost:8200))(/api(?:/[A-Za-z0-9_.<>{}-]+)*)""", re.M)


def _page(name: str) -> str:
    return (DOCS_DIR / name).read_text(encoding="utf-8")


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code blocks."""
    lines, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return lines


def _slugify(text: str) -> str:
    """Python-Markdown's default ``toc`` slug of a heading's text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> their text
    text = re.sub(r"[`*]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    for line in _prose_lines(text):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        slug = _slugify(match.group(2))
        candidate, n = slug, 0
        while candidate in anchors:  # duplicate headings get _1, _2, ...
            n += 1
            candidate = f"{slug}_{n}"
        anchors.add(candidate)
    return anchors


def _headings(text: str, level: int) -> list[str]:
    found = []
    for line in _prose_lines(text):
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == level:
            found.append(match.group(2))
    return found


def _nav_pages(node) -> list[str]:
    if isinstance(node, str):
        return [node] if node.endswith(".md") else []
    if isinstance(node, list):
        return [page for item in node for page in _nav_pages(item)]
    if isinstance(node, dict):
        return [page for value in node.values() for page in _nav_pages(value)]
    return []


def _doc_pages() -> list[str]:
    return sorted(p.relative_to(DOCS_DIR).as_posix() for p in DOCS_DIR.rglob("*.md"))


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in _prose_lines(text):
        stripped = line.strip()
        if stripped.startswith("|") and not re.match(r"^\|[\s|:-]+\|$", stripped):
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


# ---------------------------------------------------------------------------
# Site structure
# ---------------------------------------------------------------------------


def test_mkdocs_config_is_plain_yaml_with_material_theme():
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    assert config["docs_dir"] == "docs"
    assert config["theme"]["name"] == "material"
    # Broken links and anchors must fail the strict build.
    for key in ("omitted_files", "absolute_links", "unrecognized_links", "anchors"):
        assert config["validation"][key] == "warn"


def test_every_page_is_in_the_nav_and_every_nav_entry_exists():
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    nav = _nav_pages(config["nav"])
    assert len(nav) == len(set(nav)), "a page is listed twice in the nav"
    missing = [page for page in nav if not (DOCS_DIR / page).is_file()]
    assert not missing, f"nav entries without a file: {missing}"
    orphans = sorted(set(_doc_pages()) - set(nav))
    assert not orphans, f"pages missing from the mkdocs.yml nav: {orphans}"


def test_relative_links_and_anchors_resolve():
    anchors = {page: _anchors(_page(page)) for page in _doc_pages()}
    problems = []
    for page in _doc_pages():
        for line in _prose_lines(_page(page)):
            for target in _LINK_RE.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):  # http:, https:, mailto:
                    continue
                path, _, anchor = target.partition("#")
                if path:
                    resolved = (DOCS_DIR / page).parent.joinpath(path).resolve()
                    try:
                        linked = resolved.relative_to(DOCS_DIR.resolve()).as_posix()
                    except ValueError:
                        problems.append(f"{page}: {target} points outside docs/ (use a full GitHub URL)")
                        continue
                    if not resolved.is_file():
                        problems.append(f"{page}: {target} does not exist")
                        continue
                else:
                    linked = page
                if anchor and linked.endswith(".md") and anchor not in anchors[linked]:
                    problems.append(f"{page}: {target} has no heading #{anchor}")
    assert not problems, "\n".join(problems)


def test_api_paths_in_the_guide_exist():
    from fastapi import FastAPI

    from app.api.router import api_router

    app = FastAPI()
    app.include_router(api_router)
    route_paths = list(app.openapi()["paths"])
    route_patterns = [
        re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$") for path in route_paths
    ]
    unknown = set()
    for page in _doc_pages():
        for path in _API_PATH_RE.findall(_page(page)):
            path = path.rstrip(".")
            concrete = re.sub(r"<[^>]+>", "x", path)
            if any(p.match(concrete) for p in route_patterns):
                continue
            if any(r.startswith(path.rstrip("/") + "/") for r in route_paths):
                continue  # a prefix such as /api/auth
            unknown.add(f"{page}: {path}")
    assert not unknown, f"API paths that don't exist: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# Content that must follow the code
# ---------------------------------------------------------------------------


def test_storage_page_has_a_section_per_supported_provider():
    from app.api.targets import VALID_TYPES

    sections = {
        "local": "Local path",
        "sftp": "SFTP",
        "b2": "Backblaze B2",
        "s3": "S3-compatible",
        "wasabi": "Wasabi",
        "dropbox": "Dropbox",
        "gdrive": "Google Drive",
    }
    # A provider added to (or removed from) the code needs its section here.
    assert set(sections) == set(VALID_TYPES)
    headings = _headings(_page("storage-providers.md"), 2)
    missing = [title for title in sections.values() if title not in headings]
    assert not missing, f"storage-providers.md lacks sections: {missing}"


def test_storage_page_repository_paths_match_the_code():
    from app.services.repo_paths import build_repo_path

    text = _page("storage-providers.md")
    examples = {
        "local": ({"path": "/mnt/user/backups"}, "/mnt/user/backups/arkive-repo", "`<path>/arkive-repo`"),
        "sftp": (
            {"remote_path": "/backups/arkive"},
            "rclone:ID:backups/arkive/arkive-backups",
            "`rclone:<target-id>:<remote path>/arkive-backups`",
        ),
        "b2": ({"bucket": "bkt"}, "rclone:ID:bkt/arkive-backups", "`rclone:<target-id>:<bucket>/arkive-backups`"),
        "dropbox": ({}, "rclone:ID:arkive-backups", "`rclone:<target-id>:arkive-backups`"),
    }
    for target_type, (config, expected, documented) in examples.items():
        assert build_repo_path({"id": "ID", "type": target_type, "config": config}) == expected
        assert documented in text
    # The SFTP note depends on the leading slash being dropped.
    assert "relative to the user's home directory" in text


def test_notifications_page_lists_every_event_and_its_default():
    from app.models.notifications import VALID_NOTIFICATION_TYPES
    from app.services.notification_events import DEFAULT_EVENTS, NOTIFICATION_EVENTS

    text = _page("notifications.md")
    documented = {}
    for row in _table_rows(text):
        match = re.fullmatch(r"`([a-z]+\.[a-z_]+)`", row[0])
        if match and len(row) == 4:
            documented[match.group(1)] = row
    catalog = {event["value"]: event for event in NOTIFICATION_EVENTS}
    assert set(documented) == set(catalog), "the events table must list exactly NOTIFICATION_EVENTS"
    for value, row in documented.items():
        assert row[1] == catalog[value]["label"], f"label of {value}"
        assert row[3] == ("yes" if value in DEFAULT_EVENTS else "no"), f"default of {value}"
    for channel_type in VALID_NOTIFICATION_TYPES:
        assert f"`{channel_type}`" in text, f"channel type {channel_type} is not documented"


def test_overdue_examples_match_the_freshness_rules():
    from app.services.freshness import GRACE_FRACTION, GRACE_MIN_HOURS, threshold_hours_for

    text = _page("monitoring.md")
    assert f"{GRACE_MIN_HOURS:g} hours or {GRACE_FRACTION:.0%}" in text
    examples = 0
    for row in _table_rows(text):
        schedule = re.match(r"`([^`]+)`", row[0])
        window = re.fullmatch(r"(\d+) h", row[-1])
        if schedule and window and len(row) == 3:
            examples += 1
            assert threshold_hours_for(schedule.group(1)) == float(window.group(1)), row
    assert examples >= 3


def test_documented_defaults_match_the_code():
    from app.core.security import ALLOW_NEW_KEYFILE_ENV
    from app.models.settings import SetupCompleteRequest
    from app.services.retention import DEFAULT_KEEP_DAILY, DEFAULT_KEEP_MONTHLY, DEFAULT_KEEP_WEEKLY
    from app.services.settings_store import DEFAULTS

    drills = _page("restore-drills.md")
    assert f"`{DEFAULTS['restore_drill_schedule']}`" in drills
    assert f"| Databases per drill | {DEFAULTS['restore_drill_max_databases']} |" in drills
    assert f"| Max dump size (MB) | {DEFAULTS['restore_drill_max_dump_mb']} |" in drills

    wizard = _page("setup-wizard.md")
    fields = SetupCompleteRequest.model_fields
    for job, field in (("Full Backup", "cloud_sync_schedule"), ("DB Dumps", "db_dump_schedule")):
        assert f"| {job} |" in wizard
        assert f"`{fields[field].default}`" in wizard, field
    assert f"`{fields['flash_schedule'].default}`" in wizard

    storage = _page("storage-providers.md")
    assert (
        f"keep {DEFAULT_KEEP_DAILY} daily, {DEFAULT_KEEP_WEEKLY} weekly and {DEFAULT_KEEP_MONTHLY} monthly" in storage
    )

    assert ALLOW_NEW_KEYFILE_ENV in _page("troubleshooting.md")


# ---------------------------------------------------------------------------
# Publishing and hygiene
# ---------------------------------------------------------------------------


def test_docs_workflow_publishes_from_main_only():
    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) <= {"push", "workflow_dispatch"}, "no pull_request or other triggers"
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    for name, job in jobs.items():
        assert job.get("if") == "github.ref == 'refs/heads/main'", f"job {name} must be limited to main"
    build_commands = " ".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert "mkdocs build --strict" in build_commands
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert any(step.get("uses", "").startswith("actions/deploy-pages@") for step in jobs["deploy"]["steps"])


def test_docs_requirements_are_pinned():
    lines = [
        line.strip()
        for line in (DOCS_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", line) for line in lines), lines
    mkdocs = next(line for line in lines if line.startswith("mkdocs=="))
    assert mkdocs.startswith("mkdocs==1."), "Material for MkDocs needs MkDocs 1.x"


def test_readme_links_the_guide_and_demo_mode():
    readme = README.read_text(encoding="utf-8")
    assert "](docs/index.md)" in readme
    assert "?demo=true" in readme
    assert "?demo=true" in _page("index.md")


def test_docs_use_only_placeholder_addresses():
    allowed_ips = {"192.168.1.100", "127.0.0.1"}
    problems = []
    for page in _doc_pages():
        text = _page(page)
        for ip in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
            if ip not in allowed_ips:
                problems.append(f"{page}: {ip}")
        for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            problems.append(f"{page}: {email}")
    assert not problems, f"use placeholder values in the docs: {problems}"
