"""Tests for credential redaction utility."""

import pytest

from app.utils.redact import redact_credentials


class TestRedactCredentials:
    """Test redaction of sensitive credential patterns."""

    def test_empty_string(self):
        """Redaction of empty string returns empty string."""
        assert redact_credentials("") == ""

    def test_none_like_string(self):
        """String 'None' is left unchanged."""
        result = redact_credentials("None")
        assert result == "None"

    # MySQL/MariaDB patterns
    def test_redact_mysql_pwd_env_var(self):
        """MYSQL_PWD environment variable is redacted."""
        text = "Starting mysqldump with MYSQL_PWD=SuperSecret123"
        result = redact_credentials(text)
        assert "SuperSecret123" not in result
        assert "[REDACTED]" in result
        assert "MYSQL_PWD=" in result

    def test_redact_mysql_password_env_var(self):
        """MYSQL_PASSWORD environment variable is redacted."""
        text = "Connecting with MYSQL_PASSWORD=MyPassword456"
        result = redact_credentials(text)
        assert "MyPassword456" not in result
        assert "[REDACTED]" in result

    def test_redact_mariadb_password_env_var(self):
        """MARIADB_PASSWORD environment variable is redacted."""
        text = "Environment: MARIADB_PASSWORD=db_secret_789"
        result = redact_credentials(text)
        assert "db_secret_789" not in result
        assert "[REDACTED]" in result

    def test_redact_mysql_password_flag_long(self):
        """--password flag with value is redacted."""
        text = "mysqldump --password=SensitivePass123 --user=root"
        result = redact_credentials(text)
        assert "SensitivePass123" not in result
        assert "[REDACTED]" in result
        assert "--password=" in result

    def test_redact_mysql_password_flag_short(self):
        """Short -p flag with value is redacted."""
        text = "mysqldump -pSecretPass789 -uroot"
        result = redact_credentials(text)
        assert "SecretPass789" not in result
        assert "[REDACTED]" in result

    def test_redact_mysql_password_flag_space(self):
        """--password flag with space-separated value is redacted."""
        text = "mysqldump --password PassWithSpaces123"
        result = redact_credentials(text)
        assert "PassWithSpaces123" not in result
        assert "[REDACTED]" in result

    # MongoDB patterns
    def test_redact_mongo_root_password_env_var(self):
        """MONGO_INITDB_ROOT_PASSWORD environment variable is redacted."""
        text = "MongoDB setup: MONGO_INITDB_ROOT_PASSWORD=mongo_secret_xyz"
        result = redact_credentials(text)
        assert "mongo_secret_xyz" not in result
        assert "[REDACTED]" in result

    def test_redact_mongodump_password_flag(self):
        """mongodump --password flag is redacted."""
        text = "mongodump --username admin --password AdminSecret123 --authenticationDatabase admin"
        result = redact_credentials(text)
        assert "AdminSecret123" not in result
        assert "[REDACTED]" in result
        assert "--password" in result

    # PostgreSQL patterns
    def test_redact_postgres_password_env_var(self):
        """POSTGRES_PASSWORD environment variable is redacted."""
        text = "PG setup: POSTGRES_PASSWORD=pgpass_secret_456"
        result = redact_credentials(text)
        assert "pgpass_secret_456" not in result
        assert "[REDACTED]" in result

    def test_redact_pgpassword_env_var(self):
        """PGPASSWORD environment variable is redacted."""
        text = "Export: PGPASSWORD=pg_secret_789"
        result = redact_credentials(text)
        assert "pg_secret_789" not in result
        assert "[REDACTED]" in result

    # Generic patterns
    def test_redact_generic_db_password(self):
        """Generic DB_PASSWORD env var is redacted."""
        text = "Config: DB_PASSWORD=dbpass_123"
        result = redact_credentials(text)
        assert "dbpass_123" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_password_env_var(self):
        """Generic password=value pattern is redacted."""
        text = "Settings: password=MyGenericPassword"
        result = redact_credentials(text)
        assert "MyGenericPassword" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_passwd_env_var(self):
        """Generic passwd=value pattern is redacted."""
        text = "Config file: passwd=OldStylePassword"
        result = redact_credentials(text)
        assert "OldStylePassword" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_secret_env_var(self):
        """Generic secret=value pattern is redacted."""
        text = "Deployment: secret=MyAppSecret123"
        result = redact_credentials(text)
        assert "MyAppSecret123" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_api_key(self):
        """Generic api_key=value pattern is redacted."""
        text = "API config: api_key=sk_live_secret_key_xyz"
        result = redact_credentials(text)
        assert "sk_live_secret_key_xyz" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_apikey(self):
        """Generic apikey=value pattern is redacted."""
        text = "Configuration: apikey=live_api_secret_456"
        result = redact_credentials(text)
        assert "live_api_secret_456" not in result
        assert "[REDACTED]" in result

    def test_redact_generic_token(self):
        """Generic token=value pattern is redacted."""
        text = "Auth: token=eyJhbGciOiJIUzI1NiIs..."
        result = redact_credentials(text)
        assert "eyJhbGciOiJIUzI1NiIs..." not in result
        assert "[REDACTED]" in result

    # Authorization headers
    def test_redact_bearer_token(self):
        """Bearer token in Authorization header is redacted."""
        text = "Request headers: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_credentials(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "Authorization: Bearer [REDACTED]" in result

    def test_redact_basic_auth(self):
        """Basic auth in Authorization header is redacted."""
        text = "Request: Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQ="
        result = redact_credentials(text)
        assert "dXNlcm5hbWU6cGFzc3dvcmQ=" not in result
        assert "Authorization: Basic [REDACTED]" in result

    # Complex scenarios
    def test_multiple_patterns_in_one_text(self):
        """Multiple credential patterns in one text are all redacted."""
        text = """
        Error executing: mysqldump -pMyPassword123 -uroot database
        Environment: MYSQL_PWD=OtherPassword456
        Config: password=ThirdPassword789
        """
        result = redact_credentials(text)
        assert "MyPassword123" not in result
        assert "OtherPassword456" not in result
        assert "ThirdPassword789" not in result
        assert result.count("[REDACTED]") >= 3

    def test_credentials_in_error_message(self):
        """Full error message with embedded credentials is redacted."""
        text = (
            "Error: connection failed with password=admin123 at host database.example.com "
            "using api_key=sk_test_secret_key. Check POSTGRES_PASSWORD=pgpass_456 env var."
        )
        result = redact_credentials(text)
        assert "admin123" not in result
        assert "sk_test_secret_key" not in result
        assert "pgpass_456" not in result
        assert "[REDACTED]" in result

    def test_text_without_credentials_unchanged(self):
        """Text without credentials is left unchanged."""
        text = "Normal error message with no secrets"
        result = redact_credentials(text)
        assert result == text

    def test_case_insensitive_redaction(self):
        """Redaction is case-insensitive for env var names."""
        text1 = "mysql_pwd=secret123"
        text2 = "MYSQL_PWD=secret123"
        text3 = "MySql_Pwd=secret123"

        result1 = redact_credentials(text1)
        result2 = redact_credentials(text2)
        result3 = redact_credentials(text3)

        assert "secret123" not in result1
        assert "secret123" not in result2
        assert "secret123" not in result3
