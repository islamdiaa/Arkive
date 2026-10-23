"""Critical tests for credential redaction in error messages.

Validates that passwords, tokens, and credentials are never exposed
in log output, error messages, or API responses. This is a data safety
gate -- any failure here is a potential credential leak.
"""

import logging

import pytest

from app.utils.redact import redact_credentials

logger = logging.getLogger(__name__)


class TestPasswordRedaction:
    """Test that all password patterns are redacted."""

    @pytest.mark.parametrize("text,secret", [
        ("MYSQL_PWD=SuperSecret123", "SuperSecret123"),
        ("MYSQL_PASSWORD=MyPassword456", "MyPassword456"),
        ("MARIADB_PASSWORD=db_secret_789", "db_secret_789"),
        ("POSTGRES_PASSWORD=pgpass_secret", "pgpass_secret"),
        ("PGPASSWORD=pg_env_pass", "pg_env_pass"),
        ("MONGO_INITDB_ROOT_PASSWORD=mongo_pass", "mongo_pass"),
        ("DB_PASSWORD=generic_db_pass", "generic_db_pass"),
    ])
    def test_env_var_passwords_redacted(self, text, secret):
        """Environment variable passwords must be fully redacted."""
        result = redact_credentials(text)
        assert secret not in result, f"Secret '{secret}' was NOT redacted from: {result}"
        assert "[REDACTED]" in result
        logger.info("Redacted env var: %s -> %s", text[:30], result[:50])

    @pytest.mark.parametrize("text,secret", [
        ("mysqldump --password=SensitivePass123 --user=root", "SensitivePass123"),
        ("mysqldump -pSecretPass789 -uroot", "SecretPass789"),
        ("mysqldump --password PassWithSpaces", "PassWithSpaces"),
        ("mongodump --password AdminSecret --authenticationDatabase admin", "AdminSecret"),
    ])
    def test_cli_flag_passwords_redacted(self, text, secret):
        """Command-line password arguments must be fully redacted."""
        result = redact_credentials(text)
        assert secret not in result, f"Secret '{secret}' was NOT redacted from: {result}"
        assert "[REDACTED]" in result
        logger.info("Redacted CLI flag: %s -> %s", text[:40], result[:60])


class TestTokenAndKeyRedaction:
    """Test that API keys, tokens, and auth headers are redacted."""

    @pytest.mark.parametrize("text,secret", [
        ("api_key=sk_live_secret_key_xyz", "sk_live_secret_key_xyz"),
        ("apikey=live_api_secret_456", "live_api_secret_456"),
        ("token=eyJhbGciOiJIUzI1NiIs", "eyJhbGciOiJIUzI1NiIs"),
        ("secret=MyAppSecret123", "MyAppSecret123"),
    ])
    def test_api_keys_and_tokens_redacted(self, text, secret):
        """API keys, tokens, and secrets must be redacted."""
        result = redact_credentials(text)
        assert secret not in result, f"Secret '{secret}' was NOT redacted from: {result}"
        assert "[REDACTED]" in result
        logger.info("Redacted key/token: %s -> %s", text[:30], result[:50])

    def test_bearer_token_redacted(self):
        """Bearer token in Authorization header must be redacted."""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_credentials(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "Authorization: Bearer [REDACTED]" in result
        logger.info("Bearer token redacted")

    def test_basic_auth_redacted(self):
        """Basic auth in Authorization header must be redacted."""
        text = "Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQ="
        result = redact_credentials(text)
        assert "dXNlcm5hbWU6cGFzc3dvcmQ=" not in result
        assert "Authorization: Basic [REDACTED]" in result
        logger.info("Basic auth redacted")


class TestMultipleCredentials:
    """Test redaction when multiple credentials appear in one message."""

    def test_multiple_patterns_all_redacted(self):
        """All credential patterns in a single message must be redacted."""
        text = (
            "Error: MYSQL_PWD=secret1 password=secret2 "
            "POSTGRES_PASSWORD=secret3 api_key=secret4"
        )
        result = redact_credentials(text)
        for secret in ["secret1", "secret2", "secret3", "secret4"]:
            assert secret not in result, f"Secret '{secret}' leaked in: {result}"
        assert result.count("[REDACTED]") >= 4
        logger.info("Multiple credentials redacted: %s", result)

    def test_error_message_with_credentials_preserves_context(self):
        """Redaction should preserve the non-credential parts of the message."""
        text = "Error: connection failed with password=admin123 at host database.example.com"
        result = redact_credentials(text)
        assert "admin123" not in result
        assert "Error: connection failed" in result
        assert "database.example.com" in result
        logger.info("Context preserved after redaction: %s", result)


class TestSafeInputs:
    """Test that non-credential text is left unchanged."""

    def test_empty_string_unchanged(self):
        """Empty string should return empty string."""
        assert redact_credentials("") == ""

    def test_normal_text_unchanged(self):
        """Normal text without credentials should be unchanged."""
        text = "Backup completed successfully in 42 seconds"
        assert redact_credentials(text) == text
        logger.info("Normal text unchanged")

    def test_case_insensitive_redaction(self):
        """Redaction should be case-insensitive."""
        for variant in ["MYSQL_PWD=secret", "mysql_pwd=secret", "Mysql_Pwd=secret"]:
            result = redact_credentials(variant)
            assert "secret" not in result
        logger.info("Case-insensitive redaction verified")


class TestSubprocessRunnerRedaction:
    """Test that subprocess_runner applies redaction to stderr logging."""

    def test_redact_in_subprocess_error_context(self):
        """Simulate a subprocess stderr message with credentials."""
        stderr = (
            "mysqldump: [ERROR] Access denied for user 'root'@'localhost' "
            "(using password: YES) with password=TopSecretPass123"
        )
        result = redact_credentials(stderr)
        assert "TopSecretPass123" not in result
        assert "[REDACTED]" in result
        assert "Access denied" in result
        logger.info("Subprocess stderr redacted: %s", result[:80])
