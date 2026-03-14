"""
API integration tests for full CRUD on the /api/directories endpoints.

Tests: POST create, GET list, PUT update, DELETE remove.
"""

import os

import pytest

from tests.conftest import auth_headers, do_setup

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Auth guard tests
# ---------------------------------------------------------------------------


async def test_directories_list_requires_auth(client):
    """GET /api/directories should require authentication after setup."""
    await do_setup(client)
    resp = await client.get("/api/directories")
    assert resp.status_code in (401, 403)


async def test_directories_create_requires_auth(client):
    """POST /api/directories should require authentication after setup."""
    await do_setup(client)
    resp = await client.post("/api/directories", json={"path": "/tmp", "label": "Temp"})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/directories — list
# ---------------------------------------------------------------------------


async def test_directories_list_empty(client):
    """GET /api/directories returns empty list initially."""
    data = await do_setup(client)
    resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "directories" in body
    assert isinstance(body["directories"], list)
    assert len(body["directories"]) == 0
    assert "items" in body
    assert body["items"] == body["directories"]
    assert body["total"] == 0


async def test_directories_list_response_structure(client):
    """GET /api/directories returns all expected top-level keys."""
    data = await do_setup(client)
    resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    required_keys = {"items", "directories", "total", "limit", "offset", "has_more"}
    assert required_keys.issubset(body.keys()), f"Missing keys: {required_keys - set(body.keys())}"


# ---------------------------------------------------------------------------
# POST /api/directories — create
# ---------------------------------------------------------------------------


async def test_directories_create(client, tmp_path):
    """POST /api/directories creates a watched directory."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "test_watched")
    os.makedirs(dir_path, exist_ok=True)

    resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Test Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["path"] == dir_path


async def test_directories_create_nonexistent_path(client):
    """POST /api/directories with non-existent path returns 400."""
    data = await do_setup(client)
    resp = await client.post(
        "/api/directories",
        json={"path": "/nonexistent/path/abc123", "label": "Bad Path"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 400


async def test_directories_create_duplicate(client, tmp_path):
    """POST /api/directories with duplicate path returns 409."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "dup_dir")
    os.makedirs(dir_path, exist_ok=True)

    # First create succeeds
    resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "First"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 201

    # Duplicate returns 409
    resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Second"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 409


async def test_directories_create_appears_in_list(client, tmp_path):
    """A created directory should appear in the list endpoint."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "listed_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Listed Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    created_id = create_resp.json()["id"]

    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    assert list_resp.status_code == 200
    body = list_resp.json()
    ids = [d["id"] for d in body["directories"]]
    assert created_id in ids
    assert body["total"] == 1


async def test_directories_create_with_exclude_patterns(client, tmp_path):
    """POST /api/directories with exclude_patterns saves them correctly."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "excl_dir")
    os.makedirs(dir_path, exist_ok=True)

    resp = await client.post(
        "/api/directories",
        json={
            "path": dir_path,
            "label": "Exclude Test",
            "exclude_patterns": ["*.log", "cache/"],
        },
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 201

    # Verify exclude patterns are saved
    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    dirs = list_resp.json()["directories"]
    assert len(dirs) == 1
    assert dirs[0]["exclude_patterns"] == ["*.log", "cache/"]


# ---------------------------------------------------------------------------
# PUT /api/directories/:id — update
# ---------------------------------------------------------------------------


async def test_directories_update(client, tmp_path):
    """PUT /api/directories/:id updates a watched directory."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "update_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Original Label"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    # Update the label
    update_resp = await client.put(
        f"/api/directories/{dir_id}",
        json={"path": dir_path, "label": "Updated Label", "exclude_patterns": ["*.tmp"]},
        headers=auth_headers(data["api_key"]),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "updated"

    # Verify update applied
    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    dirs = list_resp.json()["directories"]
    updated = [d for d in dirs if d["id"] == dir_id][0]
    assert updated["label"] == "Updated Label"
    assert updated["exclude_patterns"] == ["*.tmp"]


async def test_directories_update_not_found(client):
    """PUT /api/directories/:id with invalid id returns 404."""
    data = await do_setup(client)
    resp = await client.put(
        "/api/directories/nonexistent",
        json={"path": "/tmp", "label": "Nope"},
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 404


async def test_directories_update_toggle_enabled(client, tmp_path):
    """PUT /api/directories/:id can disable a directory."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "toggle_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Toggle Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    # Disable it
    update_resp = await client.put(
        f"/api/directories/{dir_id}",
        json={"path": dir_path, "label": "Toggle Dir", "enabled": False},
        headers=auth_headers(data["api_key"]),
    )
    assert update_resp.status_code == 200

    # Verify disabled
    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    dirs = list_resp.json()["directories"]
    toggled = [d for d in dirs if d["id"] == dir_id][0]
    assert toggled["enabled"] is False


async def test_directories_update_rejects_relative_path(client, tmp_path):
    """PUT /api/directories/:id should reject relative paths like create does."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "good_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Good Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/directories/{dir_id}",
        json={"path": "relative-path", "label": "Bad Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert update_resp.status_code == 400
    assert "absolute" in update_resp.text.lower()


async def test_directories_update_rejects_nonexistent_path(client, tmp_path):
    """PUT /api/directories/:id should reject non-existent paths like create does."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "existing_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Existing Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    missing_path = str(tmp_path / "missing_dir")
    update_resp = await client.put(
        f"/api/directories/{dir_id}",
        json={"path": missing_path, "label": "Missing Dir"},
        headers=auth_headers(data["api_key"]),
    )
    assert update_resp.status_code == 400
    assert "does not exist" in update_resp.text.lower()


# ---------------------------------------------------------------------------
# DELETE /api/directories/:id — remove
# ---------------------------------------------------------------------------


async def test_directories_delete(client, tmp_path):
    """DELETE /api/directories/:id removes a watched directory."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "del_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Delete Me"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    # Delete
    del_resp = await client.delete(
        f"/api/directories/{dir_id}",
        headers=auth_headers(data["api_key"]),
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # Verify removed from list
    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    ids = [d["id"] for d in list_resp.json()["directories"]]
    assert dir_id not in ids


async def test_directories_delete_not_found(client):
    """DELETE /api/directories/:id with invalid id returns 404."""
    data = await do_setup(client)
    resp = await client.delete(
        "/api/directories/nonexistent",
        headers=auth_headers(data["api_key"]),
    )
    assert resp.status_code == 404


async def test_directories_delete_idempotent(client, tmp_path):
    """Deleting the same directory twice returns 404 the second time."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "idem_dir")
    os.makedirs(dir_path, exist_ok=True)

    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "Idempotent"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    # First delete succeeds
    resp1 = await client.delete(
        f"/api/directories/{dir_id}",
        headers=auth_headers(data["api_key"]),
    )
    assert resp1.status_code == 200

    # Second delete returns 404
    resp2 = await client.delete(
        f"/api/directories/{dir_id}",
        headers=auth_headers(data["api_key"]),
    )
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Full CRUD round-trip
# ---------------------------------------------------------------------------


async def test_directories_full_crud_roundtrip(client, tmp_path):
    """Full create -> read -> update -> delete cycle."""
    data = await do_setup(client)
    dir_path = str(tmp_path / "crud_dir")
    os.makedirs(dir_path, exist_ok=True)

    # CREATE
    create_resp = await client.post(
        "/api/directories",
        json={"path": dir_path, "label": "CRUD Test"},
        headers=auth_headers(data["api_key"]),
    )
    assert create_resp.status_code == 201
    dir_id = create_resp.json()["id"]

    # READ
    list_resp = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    assert list_resp.status_code == 200
    dirs = list_resp.json()["directories"]
    assert any(d["id"] == dir_id for d in dirs)

    # UPDATE
    update_resp = await client.put(
        f"/api/directories/{dir_id}",
        json={"path": dir_path, "label": "Updated CRUD"},
        headers=auth_headers(data["api_key"]),
    )
    assert update_resp.status_code == 200

    # Verify update
    list_resp2 = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    updated = [d for d in list_resp2.json()["directories"] if d["id"] == dir_id][0]
    assert updated["label"] == "Updated CRUD"

    # DELETE
    del_resp = await client.delete(
        f"/api/directories/{dir_id}",
        headers=auth_headers(data["api_key"]),
    )
    assert del_resp.status_code == 200

    # Verify gone
    list_resp3 = await client.get("/api/directories", headers=auth_headers(data["api_key"]))
    assert all(d["id"] != dir_id for d in list_resp3.json()["directories"])
