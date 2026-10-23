"""Integration tests for credential redaction in logging.

Tests that credentials are not logged when database dump operations fail.
"""

import logging
import pytest

from app.utils.redact import redact_credentials


@pytest.mark.integration
class TestCredentialRedactionInLogs:
    """Test credential redaction in database dump error scenarios."""

    def test_mysql_error_does_not_expose_password_in_logs(self):
        """MySQL dump failure does not expose password in logs."""
        # Simulate error message with credentials in env var format
        error_output = "Error: Access denied for user 'root' with MYSQL_PWD=SuperSecretPassword123"
        redacted = redact_credentials(error_output)

        # Verify password is not in output (redacted)
        assert "SuperSecretPassword123" not in redacted
        # But we should see the main error message
        assert "Error: Access denied" in redacted
        assert "[REDACTED]" in redacted

    def test_postgres_error_does_not_expose_password_in_logs(self):
        """Postgres dump failure does not expose password in logs."""
        error_output = "Error: password authentication failed. POSTGRES_PASSWORD=pgpass_secret_789"
        redacted = redact_credentials(error_output)

        # Verify password is not in logs
        assert "pgpass_secret_789" not in redacted
        assert "Error:" in redacted
        assert "[REDACTED]" in redacted

    def test_mongodb_error_does_not_expose_password_in_logs(self):
        """MongoDB dump failure does not expose password in logs."""
        error_output = "Authentication failed with MONGO_INITDB_ROOT_PASSWORD=mongo_secret_xyz_123"
        redacted = redact_credentials(error_output)

        # Verify password is not in logs
        assert "mongo_secret_xyz_123" not in redacted
        assert "Authentication failed" in redacted
        assert "[REDACTED]" in redacted

    def test_exception_does_not_expose_credentials(self):
        """Exceptions do not expose credentials in error messages."""
        error_message = "Failed to connect using password=admin_password_123 to host database.example.com"
        redacted = redact_credentials(error_message)

        # Verify password is not in error message
        assert "admin_password_123" not in redacted
        assert "[REDACTED]" in redacted

    def test_subprocess_stderr_redaction(self):
        """Test redaction in subprocess_runner error logging."""
        # Simulate stderr output from a failed mysqldump
        stderr = "mysqldump: [ERROR] Access denied for user 'root'@'localhost' (using password: YES) with password=TopSecretPass123"
        redacted = redact_credentials(stderr)

        assert "TopSecretPass123" not in redacted
        assert "[REDACTED]" in redacted

    def test_environment_variable_exposure_prevented(self):
        """Environment variables containing credentials are redacted."""
        env_output = "MYSQL_PWD=secretPass123 DATABASE_PASSWORD=dbPass456 POSTGRES_PASSWORD=pgPass789"
        redacted = redact_credentials(env_output)

        assert "secretPass123" not in redacted
        assert "dbPass456" not in redacted
        assert "pgPass789" not in redacted
        assert "[REDACTED]" in redacted

    def test_command_line_arguments_redacted(self):
        """Command-line arguments with passwords are redacted."""
        cmd_output = "mysqldump --user=root --password=MyPassword123 --database=mydb"
        redacted = redact_credentials(cmd_output)

        assert "MyPassword123" not in redacted
        assert "--user=root" in redacted
        assert "--database=mydb" in redacted
        assert "[REDACTED]" in redacted
