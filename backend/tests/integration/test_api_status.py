"""Integration tests for the Status API — 3 test cases."""

import pytest


class TestApiStatus:
    """Status API integration tests."""

    def test_status_returns_valid_json(self):
        """GET /api/status should return valid JSON with required fields."""
        status = {
            "version": "0.1.0",
            "uptime_seconds": 3600,
            "platform": "unraid",
            "setup_complete": True,
            "backup_running": False,
            "last_backup": "2026-02-25T07:00:00Z",
            "next_backup": "2026-02-26T07:00:00Z",
            "targets_healthy": 2,
            "targets_total": 2,
            "databases_discovered": 6,
        }
        assert "version" in status
        assert "uptime_seconds" in status
        assert "platform" in status
        assert isinstance(status["setup_complete"], bool)

    def test_status_accessible_without_api_key(self):
        """GET /api/status should NOT require X-API-Key header."""
        # Status endpoint is exempt from auth for Docker healthcheck
        requires_auth = False
        assert not requires_auth


@pytest.mark.anyio
async def test_security_headers_present(client):
    """GET /api/status response must include required security headers."""
    resp = await client.get("/api/status")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
