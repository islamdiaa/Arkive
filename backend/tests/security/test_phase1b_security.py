"""Tests for Phase 1B security fixes.

Covers:
1. Credential redaction (redact.py)
2. CORS configuration (config.py + main.py)
3. Cron validation (cron_validation.py + auth.py + jobs.py)
4. Rate limiting thread safety (dependencies.py)
"""

import threading

import pytest
from tests.conftest import auth_headers, do_setup


# ---------------------------------------------------------------------------
# 1. Credential Redaction
# ---------------------------------------------------------------------------


class TestCredentialRedaction:
    def test_mysql_pwd_redacted(self):
        from app.utils.redact import redact_credentials

        text = "MYSQL_PWD=supersecret123 mysqldump failed"
        result = redact_credentials(text)
        assert "supersecret123" not in result
        assert "MYSQL_PWD=[REDACTED]" in result

    def test_password_flag_redacted(self):
        from app.utils.redact import redact_credentials

        text = "--password=mypass123 --host=db"
        result = redact_credentials(text)
        assert "mypass123" not in result
        assert "--password=[REDACTED]" in result

    def test_password_flag_space_separated(self):
        from app.utils.redact import redact_credentials

        text = "--password secretval --host=db"
        result = redact_credentials(text)
        assert "secretval" not in result
        assert "--password [REDACTED]" in result

    def test_postgres_password_redacted(self):
        from app.utils.redact import redact_credentials

        text = "POSTGRES_PASSWORD=pg_pass_123 pg_dump failed"
        result = redact_credentials(text)
        assert "pg_pass_123" not in result

    def test_db_password_redacted(self):
        from app.utils.redact import redact_credentials

        text = "DB_PASSWORD=dbpass error occurred"
        result = redact_credentials(text)
        assert "dbpass" not in result
        assert "DB_PASSWORD=[REDACTED]" in result

    def test_mongo_pass_redacted(self):
        from app.utils.redact import redact_credentials

        text = "MONGO_INITDB_ROOT_PASSWORD=mongo_secret mongodump failed"
        result = redact_credentials(text)
        assert "mongo_secret" not in result

    def test_empty_string_returns_empty(self):
        from app.utils.redact import redact_credentials

        assert redact_credentials("") == ""
        assert redact_credentials(None) is None

    def test_no_credentials_unchanged(self):
        from app.utils.redact import redact_credentials

        text = "pg_dump: error connecting to host localhost"
        assert redact_credentials(text) == text

    def test_mariadb_password_redacted(self):
        from app.utils.redact import redact_credentials

        text = "MARIADB_PASSWORD=maria_secret connection refused"
        result = redact_credentials(text)
        assert "maria_secret" not in result


# ---------------------------------------------------------------------------
# 2. CORS Configuration
# ---------------------------------------------------------------------------


class TestCORSConfiguration:
    def test_default_cors_origins(self):
        from app.core.config import ArkiveConfig

        config = ArkiveConfig()
        assert "http://localhost:5173" in config.cors_origins
        assert "http://localhost:8200" in config.cors_origins
        assert "http://127.0.0.1:5173" in config.cors_origins
        assert "http://127.0.0.1:8200" in config.cors_origins

    def test_cors_origins_from_comma_separated_string(self):
        from app.core.config import ArkiveConfig

        config = ArkiveConfig(cors_origins="http://myhost:8200,http://other:3000")
        assert config.cors_origins == ["http://myhost:8200", "http://other:3000"]

    def test_cors_origins_strips_whitespace(self):
        from app.core.config import ArkiveConfig

        config = ArkiveConfig(cors_origins=" http://a:1 , http://b:2 ")
        assert config.cors_origins == ["http://a:1", "http://b:2"]

    def test_cors_origins_list_passthrough(self):
        from app.core.config import ArkiveConfig

        origins = ["http://custom:9000"]
        config = ArkiveConfig(cors_origins=origins)
        assert config.cors_origins == ["http://custom:9000"]

    async def test_cors_header_present(self, client):
        resp = await client.options(
            "/api/status",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------------------------------------------------------------------------
# 3. Cron Validation
# ---------------------------------------------------------------------------


class TestCronValidation:
    def test_valid_cron_accepted(self):
        from app.utils.cron_validation import validate_cron_expression

        result = validate_cron_expression("0 6 * * *")
        assert result == "0 6 * * *"

    def test_valid_cron_with_ranges(self):
        from app.utils.cron_validation import validate_cron_expression

        result = validate_cron_expression("*/5 1-3 * * 1,3,5")
        assert result == "*/5 1-3 * * 1,3,5"

    def test_invalid_cron_too_few_fields(self):
        from fastapi import HTTPException

        from app.utils.cron_validation import validate_cron_expression

        with pytest.raises(HTTPException) as exc_info:
            validate_cron_expression("0 6 *")
        assert exc_info.value.status_code == 422
        assert "expected 5 fields" in exc_info.value.detail

    def test_invalid_cron_too_many_fields(self):
        from fastapi import HTTPException

        from app.utils.cron_validation import validate_cron_expression

        with pytest.raises(HTTPException) as exc_info:
            validate_cron_expression("0 6 * * * *")
        assert exc_info.value.status_code == 422

    def test_invalid_cron_bad_values(self):
        from fastapi import HTTPException

        from app.utils.cron_validation import validate_cron_expression

        with pytest.raises(HTTPException) as exc_info:
            validate_cron_expression("99 99 99 99 99")
        assert exc_info.value.status_code == 422

    def test_cron_strips_whitespace(self):
        from app.utils.cron_validation import validate_cron_expression

        result = validate_cron_expression("  0 6 * * *  ")
        assert result == "0 6 * * *"

    async def test_create_job_invalid_cron_returns_422(self, client):
        data = await do_setup(client)
        api_key = data["api_key"]

        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Bad Schedule",
                "type": "full",
                "schedule": "not-a-cron",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 422

    async def test_create_job_valid_cron_accepted(self, client):
        data = await do_setup(client)
        api_key = data["api_key"]

        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Good Schedule",
                "type": "full",
                "schedule": "0 3 * * *",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 201

    async def test_update_job_invalid_cron_returns_422(self, client):
        data = await do_setup(client)
        api_key = data["api_key"]

        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Update Test",
                "type": "full",
                "schedule": "0 3 * * *",
            },
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]

        resp = await client.put(
            f"/api/jobs/{job_id}",
            json={"schedule": "not-valid-cron"},
            headers=auth_headers(api_key),
        )
        assert resp.status_code == 422

    async def test_setup_invalid_cron_returns_422(self, client):
        resp = await client.post(
            "/api/auth/setup",
            json={
                "encryption_password": "test-password",
                "db_dump_schedule": "invalid-cron",
            },
        )
        assert resp.status_code == 422

    async def test_setup_invalid_cron_via_schedules_dict_returns_422(self, client):
        resp = await client.post(
            "/api/auth/setup",
            json={
                "encryption_password": "test-password",
                "schedules": {"db_dump": "not-a-cron"},
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 4. Rate Limiting Thread Safety
# ---------------------------------------------------------------------------


class TestRateLimitingThreadSafety:
    def test_concurrent_failed_attempts_all_tracked(self):
        from app.core.dependencies import (
            RATE_LIMIT_MAX,
            _failed_attempts,
            _is_locked_out,
            _lockouts,
            _track_failed_attempt,
            clear_rate_limit,
        )

        ip = "10.0.0.99"
        clear_rate_limit(ip)

        barrier = threading.Barrier(RATE_LIMIT_MAX)
        errors = []

        def attempt():
            try:
                barrier.wait(timeout=5)
                _track_failed_attempt(ip)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=attempt) for _ in range(RATE_LIMIT_MAX)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert _is_locked_out(ip), "IP should be locked out after concurrent failed attempts"

        # Clean up
        clear_rate_limit(ip)

    def test_concurrent_clear_does_not_raise(self):
        from app.core.dependencies import (
            _track_failed_attempt,
            clear_rate_limit,
        )

        ip = "10.0.0.100"
        _track_failed_attempt(ip)

        errors = []

        def clear():
            try:
                clear_rate_limit(ip)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=clear) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors

    def test_lock_exists_on_module(self):
        from app.core.dependencies import _rate_limit_lock

        assert isinstance(_rate_limit_lock, type(threading.Lock()))
