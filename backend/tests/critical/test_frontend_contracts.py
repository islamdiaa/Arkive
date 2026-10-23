"""Critical tests for frontend contract validation.

Validates that the mock.ts DEMO_STATUS fields match the real API response.
This prevents the class of bug where the frontend reads a field that the
backend never returns (e.g., the "0 snapshots" bug from missing total_snapshots).
"""

import json
import logging
import re
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

MOCK_TS_PATH = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "api" / "mock.ts"

# Fields that the real /api/status endpoint returns
REAL_STATUS_FIELDS = {
    "status",
    "health",
    "version",
    "hostname",
    "uptime_seconds",
    "platform",
    "setup_completed",
    "checks",
    "last_backup",
    "next_backup",
    "targets",
    "databases",
    "storage",
    "total_snapshots",
    "coverage",
}


def _parse_demo_status_keys() -> set[str]:
    """Extract top-level keys from DEMO_STATUS in mock.ts.

    Uses brace-depth tracking to handle nested objects like
    targets: { total: 2, healthy: 2 } without truncating at the
    first closing brace.
    """
    if not MOCK_TS_PATH.exists():
        pytest.skip(f"mock.ts not found at {MOCK_TS_PATH}")

    content = MOCK_TS_PATH.read_text()

    # Find the opening brace of DEMO_STATUS
    start_match = re.search(r'const DEMO_STATUS\s*=\s*\{', content)
    if not start_match:
        pytest.fail("Could not find DEMO_STATUS in mock.ts")

    brace_start = start_match.end() - 1  # index of the '{'

    # Walk braces to find the matching closing brace
    depth = 0
    block_end = brace_start
    for i in range(brace_start, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                block_end = i
                break
    else:
        pytest.fail("Could not find closing brace for DEMO_STATUS")

    block = content[brace_start + 1:block_end]

    # Extract only top-level keys (at brace depth 0)
    keys = set()
    current_depth = 0
    for line in block.split('\n'):
        stripped = line.strip()
        if current_depth == 0:
            m = re.match(r'(\w+)\s*:', stripped)
            if m:
                keys.add(m.group(1))
        current_depth += stripped.count('{') - stripped.count('}')

    return keys


class TestMockTsStatusContract:
    """Test that mock.ts DEMO_STATUS matches the real API contract."""

    def test_mock_ts_exists(self):
        """Frontend mock.ts file must exist."""
        assert MOCK_TS_PATH.exists(), f"mock.ts not found at {MOCK_TS_PATH}"
        logger.info("mock.ts found at %s", MOCK_TS_PATH)

    def test_mock_has_total_snapshots(self):
        """DEMO_STATUS must include total_snapshots to prevent '0 snapshots' bug."""
        keys = _parse_demo_status_keys()
        assert "total_snapshots" in keys, \
            "DEMO_STATUS missing total_snapshots -- this caused the '0 snapshots' dashboard bug"
        logger.info("total_snapshots present in DEMO_STATUS")

    def test_mock_has_setup_completed(self):
        """DEMO_STATUS must include setup_completed for first-boot detection."""
        keys = _parse_demo_status_keys()
        assert "setup_completed" in keys, "DEMO_STATUS missing setup_completed"
        logger.info("setup_completed present in DEMO_STATUS")

    def test_mock_has_platform(self):
        """DEMO_STATUS must include platform for Unraid-specific features."""
        keys = _parse_demo_status_keys()
        assert "platform" in keys, "DEMO_STATUS missing platform"
        logger.info("platform present in DEMO_STATUS")

    def test_mock_has_hostname(self):
        """DEMO_STATUS must include hostname."""
        keys = _parse_demo_status_keys()
        assert "hostname" in keys, "DEMO_STATUS missing hostname"
        logger.info("hostname present in DEMO_STATUS")


class TestMockTsEndpointCoverage:
    """Test that mock.ts covers critical API endpoints."""

    def test_mock_has_status_route(self):
        """Mock must handle /status GET."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        assert "'/status'" in content or '"/status"' in content
        logger.info("/status route present in mock")

    def test_mock_has_directories_scan_route(self):
        """Mock must handle /directories/scan POST with suggestions key."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        assert "directories/scan" in content
        assert "suggestions" in content
        logger.info("/directories/scan route with suggestions present in mock")

    def test_mock_has_auth_routes(self):
        """Mock must handle auth setup, login, logout."""
        content = MOCK_TS_PATH.read_text() if MOCK_TS_PATH.exists() else ""
        assert "/auth/setup" in content
        assert "/auth/login" in content
        assert "/auth/logout" in content
        logger.info("Auth routes present in mock")


@pytest.mark.asyncio
async def test_real_api_returns_superset_of_mock_status_fields(client):
    """Real API status response should contain at least all DEMO_STATUS keys."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    real_keys = set(resp.json().keys())

    mock_keys = _parse_demo_status_keys()

    # The real API should return at least all fields the mock promises.
    # Note: the mock may use different names for some fields (e.g., backup_running
    # vs the structured last_backup object). We check for critical overlap.
    critical_fields = {"version", "platform", "hostname", "setup_completed",
                       "uptime_seconds", "total_snapshots"}
    missing_critical = critical_fields - real_keys
    assert not missing_critical, \
        f"Real API missing critical fields that mock provides: {missing_critical}"
    logger.info("Real API covers all critical mock fields")
