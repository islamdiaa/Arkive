"""Tests for FastAPI /docs exposure."""

import pytest


@pytest.mark.asyncio
async def test_openapi_json_accessible(client):
    """GET /openapi.json should return the OpenAPI schema, not a frontend page."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "openapi" in data
    assert "paths" in data


@pytest.mark.asyncio
async def test_docs_accessible(client):
    """GET /docs should return the Swagger UI page."""
    resp = await client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_redoc_accessible(client):
    """GET /redoc should return the ReDoc page."""
    resp = await client.get("/redoc")
    assert resp.status_code == 200
