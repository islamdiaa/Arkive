"""End-to-end API tests: create → test-connection → backup → usage for every provider.

Each test exercises the real API endpoints with a mocked subprocess layer,
verifying the full lifecycle of every supported storage provider.
"""

import os

import pytest

from tests.cloud_providers.conftest import ALL_PROVIDERS

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _create_target(client, provider_key, tmp_path):
    """Create a target for the given provider, return target dict."""
    spec = ALL_PROVIDERS[provider_key].copy()
    config = spec["config"].copy()

    # Local targets need a real path
    if provider_key == "local":
        target_path = str(tmp_path / f"backup-{provider_key}")
        os.makedirs(target_path, exist_ok=True)
        config["path"] = target_path

    resp = await client.post(
        "/api/targets",
        json={
            "name": spec["name"],
            "type": spec["type"],
            "config": config,
        },
    )
    return resp


# ---------------------------------------------------------------------------
# CREATE — every provider
# ---------------------------------------------------------------------------


class TestCreateTargets:
    """POST /api/targets for every provider type."""

    @pytest.mark.parametrize("provider", list(ALL_PROVIDERS.keys()))
    async def test_create_target(self, provider_client, provider):
        resp = await _create_target(provider_client, provider, provider_client._tmp_path)
        assert resp.status_code == 201, f"{provider}: {resp.text}"
        data = resp.json()
        assert data["type"] == ALL_PROVIDERS[provider]["type"]
        assert data["name"] == ALL_PROVIDERS[provider]["name"]
        assert "id" in data

    async def test_create_all_providers_coexist(self, provider_client):
        """All supported providers can coexist in the same system."""
        for provider in ALL_PROVIDERS:
            resp = await _create_target(provider_client, provider, provider_client._tmp_path)
            assert resp.status_code == 201, f"{provider}: {resp.text}"

        # Verify all targets are listed
        list_resp = await provider_client.get("/api/targets")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["total"] == len(ALL_PROVIDERS)
        types = {t["type"] for t in data["items"]}
        for provider in ALL_PROVIDERS:
            assert ALL_PROVIDERS[provider]["type"] in types


# ---------------------------------------------------------------------------
# VALIDATION — empty config rejected for providers with required fields
# ---------------------------------------------------------------------------


class TestValidation:
    """Empty configs should be rejected for providers that require credentials."""

    @pytest.mark.parametrize(
        "provider,expected_errors",
        [
            ("b2", ["Application Key ID is required", "Application Key is required", "Bucket name is required"]),
            (
                "s3",
                [
                    "Endpoint URL is required",
                    "Access Key is required",
                    "Secret Key is required",
                    "Bucket name is required",
                ],
            ),
            ("wasabi", ["Access Key is required", "Secret Key is required", "Bucket name is required"]),
            ("sftp", ["Host is required", "Username is required"]),
            ("local", ["Path is required"]),
            ("dropbox", ["Access token is required"]),
            ("gdrive", ["Client ID is required"]),
        ],
    )
    async def test_empty_config_rejected(self, provider_client, provider, expected_errors):
        resp = await provider_client.post(
            "/api/targets",
            json={
                "name": f"Bad {provider}",
                "type": provider,
                "config": {},
            },
        )
        assert resp.status_code == 422, f"{provider}: expected 422, got {resp.status_code}"
        # The global exception handler wraps detail dict into message string
        message = resp.json().get("message", "")
        for err in expected_errors:
            assert err in message, f"{provider}: expected '{err}' in message, got: {message}"


# ---------------------------------------------------------------------------
# TEST CONNECTION — every cloud provider
# ---------------------------------------------------------------------------


class TestConnection:
    """POST /api/targets/{id}/test for every provider."""

    @pytest.mark.parametrize("provider", list(ALL_PROVIDERS.keys()))
    async def test_connection_succeeds(self, provider_client, provider):
        # Create target
        create_resp = await _create_target(provider_client, provider, provider_client._tmp_path)
        assert create_resp.status_code == 201
        target_id = create_resp.json()["id"]

        # Test connection
        test_resp = await provider_client.post(f"/api/targets/{target_id}/test")
        assert test_resp.status_code == 200
        data = test_resp.json()
        assert data["success"] is True, f"{provider}: {data}"
        assert data["message"]  # Should have a message

        # Verify status updated in DB
        get_resp = await provider_client.get(f"/api/targets/{target_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "ok"

    @pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p != "local"])
    async def test_inline_connection(self, provider_client, provider):
        """POST /api/targets/test-connection (without saving to DB)."""
        spec = ALL_PROVIDERS[provider]
        resp = await provider_client.post(
            "/api/targets/test-connection",
            json={
                "type": spec["type"],
                "config": spec["config"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True, f"{provider}: {data}"


# ---------------------------------------------------------------------------
# USAGE — cloud providers
# ---------------------------------------------------------------------------


class TestUsage:
    """GET /api/targets/{id}/usage for every provider."""

    @pytest.mark.parametrize("provider", list(ALL_PROVIDERS.keys()))
    async def test_get_usage(self, provider_client, provider):
        create_resp = await _create_target(provider_client, provider, provider_client._tmp_path)
        target_id = create_resp.json()["id"]

        usage_resp = await provider_client.get(f"/api/targets/{target_id}/usage")
        assert usage_resp.status_code == 200
        data = usage_resp.json()
        assert data["target_id"] == target_id
        assert data["type"] == ALL_PROVIDERS[provider]["type"]
        # All providers should return usage numbers
        assert "total" in data
        assert "used" in data
        assert "free" in data


# ---------------------------------------------------------------------------
# FULL LIFECYCLE — create → test → backup → snapshots → usage → delete
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Complete CRUD + operations lifecycle for each provider."""

    @pytest.mark.parametrize("provider", list(ALL_PROVIDERS.keys()))
    async def test_full_lifecycle(self, provider_client, provider):
        client = provider_client

        # 1. CREATE
        create_resp = await _create_target(client, provider, client._tmp_path)
        assert create_resp.status_code == 201
        target = create_resp.json()
        target_id = target["id"]

        # 2. GET (verify it exists)
        get_resp = await client.get(f"/api/targets/{target_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == ALL_PROVIDERS[provider]["name"]

        # 3. UPDATE name
        update_resp = await client.put(
            f"/api/targets/{target_id}",
            json={
                "name": f"Updated {provider}",
            },
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["name"] == f"Updated {provider}"

        # 4. TEST CONNECTION
        test_resp = await client.post(f"/api/targets/{target_id}/test")
        assert test_resp.status_code == 200
        assert test_resp.json()["success"] is True

        # 5. GET USAGE
        usage_resp = await client.get(f"/api/targets/{target_id}/usage")
        assert usage_resp.status_code == 200

        # 6. LIST (verify in list)
        list_resp = await client.get("/api/targets")
        assert list_resp.status_code == 200
        ids = [t["id"] for t in list_resp.json()["items"]]
        assert target_id in ids

        # 7. Verify sensitive fields are redacted in list
        targets = list_resp.json()["items"]
        our_target = next(t for t in targets if t["id"] == target_id)
        config = our_target.get("config", {})
        for key in config:
            if any(s in key.lower() for s in ("key", "secret", "password", "token")):
                assert config[key] == "***", f"{provider}: {key} not redacted"

        # 8. DELETE
        del_resp = await client.delete(f"/api/targets/{target_id}")
        assert del_resp.status_code == 200

        # 9. VERIFY DELETED
        get_resp = await client.get(f"/api/targets/{target_id}")
        assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# RCLONE CONFIG — verify correct config written per provider
# ---------------------------------------------------------------------------


class TestRcloneConfig:
    """Verify the rclone config file is correctly written for cloud providers."""

    @pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p != "local"])
    async def test_rclone_config_written_after_test(self, provider_client, provider):
        """After test-connection, rclone config should have correct section."""
        create_resp = await _create_target(provider_client, provider, provider_client._tmp_path)
        target_id = create_resp.json()["id"]

        # Test connection triggers config write
        await provider_client.post(f"/api/targets/{target_id}/test")

        # Read the rclone config
        rclone_path = provider_client._test_config.rclone_config
        assert rclone_path.exists(), f"rclone config not found for {provider}"

        content = rclone_path.read_text()
        assert f"[{target_id}]" in content, f"Section missing for {provider}"

        # Verify provider-specific config
        expected = {
            "b2": "type = b2",
            "s3": "type = s3",
            "wasabi": "provider = Wasabi",
            "sftp": "type = sftp",
            "dropbox": "type = dropbox",
            "gdrive": "type = drive",
        }
        assert expected[provider] in content, f"{provider}: expected '{expected[provider]}' in config"


# ---------------------------------------------------------------------------
# BACKUP ENGINE — verify backup works with each provider's repo path
# ---------------------------------------------------------------------------


class TestBackupEngine:
    """BackupEngine.backup() with mocked restic for each provider."""

    @pytest.mark.parametrize("provider", list(ALL_PROVIDERS.keys()))
    async def test_backup_to_provider(self, provider_client, provider):
        """Create target, init repo, run backup → success with snapshot_id."""
        from app.services.backup_engine import BackupEngine

        # Create target via API
        create_resp = await _create_target(provider_client, provider, provider_client._tmp_path)
        target = create_resp.json()
        target_dict = {
            "id": target["id"],
            "type": target["type"],
            "config": ALL_PROVIDERS[provider]["config"].copy(),
        }
        if provider == "local":
            target_dict["config"]["path"] = str(provider_client._tmp_path / f"backup-{provider}")

        engine = provider_client._test_config
        backup_engine = BackupEngine(engine)

        # Init repo
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.backup_engine.run_command", provider_client._mock_run)
            init_ok = await backup_engine.init_repo(target_dict)
            assert init_ok, f"{provider}: repo init failed"

            # Create a test file to back up
            test_dir = provider_client._tmp_path / "test-data"
            test_dir.mkdir(exist_ok=True)
            (test_dir / "hello.txt").write_text("backup this")

            # Run backup
            result = await backup_engine.backup(
                target_dict,
                paths=[str(test_dir)],
                tags=[f"provider:{provider}", "test:true"],
            )
            assert result["status"] == "success", f"{provider}: {result}"
            assert result["snapshot_id"] == "abc12345"
            assert result["files_new"] == 5

    @pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p != "local"])
    async def test_repo_path_uses_rclone(self, provider_client, provider):
        """Cloud providers should use rclone:{id}:arkive-backups repo path."""
        from app.services.backup_engine import BackupEngine

        engine = BackupEngine(provider_client._test_config)
        target = {"id": "test123", "type": ALL_PROVIDERS[provider]["type"], "config": {}}
        path = engine._repo_path(target)
        assert path == "rclone:test123:arkive-backups"

    async def test_local_repo_path(self, provider_client):
        """Local targets should use {path}/arkive-repo."""
        from app.services.backup_engine import BackupEngine

        engine = BackupEngine(provider_client._test_config)
        target = {"id": "local1", "type": "local", "config": {"path": "/mnt/backups"}}
        path = engine._repo_path(target)
        assert path == "/mnt/backups/arkive-repo"

    async def test_sftp_repo_path_honors_remote_path(self, provider_client):
        """SFTP targets should place the repo under configured remote_path."""
        from app.services.backup_engine import BackupEngine

        engine = BackupEngine(provider_client._test_config)
        target = {
            "id": "sftp1",
            "type": "sftp",
            "config": {"remote_path": "upload"},
        }
        path = engine._repo_path(target)
        assert path == "rclone:sftp1:upload/arkive-backups"

    async def test_s3_repo_path_includes_bucket(self, provider_client):
        """S3-compatible targets should scope the repo under the configured bucket."""
        from app.services.backup_engine import BackupEngine

        engine = BackupEngine(provider_client._test_config)
        target = {
            "id": "s31",
            "type": "s3",
            "config": {"bucket": "vmx-bucket"},
        }
        path = engine._repo_path(target)
        assert path == "rclone:s31:vmx-bucket/arkive-backups"

    async def test_b2_repo_path_includes_bucket_and_remote_path(self, provider_client):
        """B2/Arkive object storage should include bucket and remote_path when present."""
        from app.services.backup_engine import BackupEngine

        engine = BackupEngine(provider_client._test_config)
        target = {
            "id": "b21",
            "type": "b2",
            "config": {"bucket": "archive-bucket", "remote_path": "tenant-a"},
        }
        path = engine._repo_path(target)
        assert path == "rclone:b21:archive-bucket/tenant-a/arkive-backups"
