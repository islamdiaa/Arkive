"""Critical tests for API status endpoint: all dashboard-required fields.

Validates that the /api/status response includes every field the frontend
dashboard reads, preventing the "0 snapshots" class of bugs where the
backend omits a field the frontend expects.
"""

import logging

import pytest

logger = logging.getLogger(__name__)

# Every top-level field the dashboard reads from /api/status
REQUIRED_STATUS_FIELDS = {
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

# Required sub-fields within nested objects
REQUIRED_TARGETS_FIELDS = {"total", "healthy"}
REQUIRED_DATABASES_FIELDS = {"total", "healthy"}
REQUIRED_STORAGE_FIELDS = {"total_bytes"}
REQUIRED_CHECKS_FIELDS = {"database", "scheduler", "disk", "binaries"}


pytestmark = pytest.mark.asyncio


async def test_status_response_contains_all_required_fields(client):
    """GET /api/status must return every field the dashboard reads."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()

    missing = REQUIRED_STATUS_FIELDS - set(body.keys())
    assert not missing, f"Missing status fields: {missing}"
    logger.info("All %d required top-level fields present", len(REQUIRED_STATUS_FIELDS))


async def test_status_targets_sub_fields(client):
    """targets object must contain total and healthy."""
    resp = await client.get("/api/status")
    body = resp.json()
    targets = body["targets"]

    missing = REQUIRED_TARGETS_FIELDS - set(targets.keys())
    assert not missing, f"Missing targets sub-fields: {missing}"
    assert isinstance(targets["total"], int)
    assert isinstance(targets["healthy"], int)
    logger.info("targets sub-fields verified: total=%d, healthy=%d",
                targets["total"], targets["healthy"])


async def test_status_databases_sub_fields(client):
    """databases object must contain total and healthy."""
    resp = await client.get("/api/status")
    body = resp.json()
    databases = body["databases"]

    missing = REQUIRED_DATABASES_FIELDS - set(databases.keys())
    assert not missing, f"Missing databases sub-fields: {missing}"
    assert isinstance(databases["total"], int)
    assert isinstance(databases["healthy"], int)
    logger.info("databases sub-fields verified: total=%d, healthy=%d",
                databases["total"], databases["healthy"])


async def test_status_storage_sub_fields(client):
    """storage object must contain total_bytes."""
    resp = await client.get("/api/status")
    body = resp.json()
    storage = body["storage"]

    missing = REQUIRED_STORAGE_FIELDS - set(storage.keys())
    assert not missing, f"Missing storage sub-fields: {missing}"
    assert isinstance(storage["total_bytes"], int)
    logger.info("storage sub-fields verified: total_bytes=%d", storage["total_bytes"])


async def test_status_checks_sub_fields(client):
    """checks object must contain database, scheduler, disk, binaries."""
    resp = await client.get("/api/status")
    body = resp.json()
    checks = body["checks"]

    missing = REQUIRED_CHECKS_FIELDS - set(checks.keys())
    assert not missing, f"Missing checks sub-fields: {missing}"

    for check_name in REQUIRED_CHECKS_FIELDS:
        check = checks[check_name]
        assert "ok" in check, f"checks.{check_name} missing 'ok' key"
        assert "message" in check, f"checks.{check_name} missing 'message' key"
    logger.info("All check sub-fields verified with ok/message keys")


async def test_status_total_snapshots_is_int(client):
    """total_snapshots must be an integer, not None."""
    resp = await client.get("/api/status")
    body = resp.json()

    assert isinstance(body["total_snapshots"], int), \
        f"total_snapshots should be int, got {type(body['total_snapshots']).__name__}"
    logger.info("total_snapshots=%d (int verified)", body["total_snapshots"])


async def test_status_coverage_present(client):
    """coverage object must be present with readiness field."""
    resp = await client.get("/api/status")
    body = resp.json()

    assert "coverage" in body
    coverage = body["coverage"]
    assert "readiness" in coverage
    logger.info("coverage.readiness=%s", coverage["readiness"])


async def test_status_health_mirrors_status(client):
    """health field should be a legacy alias for status."""
    resp = await client.get("/api/status")
    body = resp.json()

    status = body["status"]
    health = body["health"]
    if status == "ok":
        assert health == "healthy"
    else:
        assert health == status
    logger.info("health=%s correctly mirrors status=%s", health, status)


async def test_status_no_auth_required(client):
    """Status endpoint must be accessible without authentication."""
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    logger.info("Status endpoint accessible without auth (200)")
