"""Credential redaction utilities for secure logging.

Redacts sensitive values from log output and error messages to prevent
credential exposure when database commands fail or subprocess operations error.
"""

import re


def redact_credentials(text: str) -> str:
    """Redact credentials from text using common patterns.

    Handles:
    - MySQL/MariaDB environment variables and command-line args
    - MongoDB environment variables and command-line args
    - PostgreSQL passwords
    - Generic DB_PASSWORD, password, and similar patterns
    - API keys and tokens

    Args:
        text: Text that may contain credentials to redact.

    Returns:
        Text with credentials replaced by [REDACTED].
    """
    if not text:
        return text

    # MySQL/MariaDB patterns
    # MYSQL_PWD=... or MYSQL_PASSWORD=... environment variable
    text = re.sub(r'(MYSQL_P(?:WD|ASSWORD))=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    # MARIADB_PASSWORD=... environment variable
    text = re.sub(r'(MARIADB_PASSWORD)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    # --password=value command-line arg (must be before -p pattern to avoid partial match)
    text = re.sub(r'(--password=)([^\s\'\"]+)', r'\1[REDACTED]', text, flags=re.IGNORECASE)
    # --password value command-line arg (space-separated) (must be before -p pattern)
    text = re.sub(r'(--password)\s+([^\-\s][^\s]*)', r'\1 [REDACTED]', text, flags=re.IGNORECASE)
    # -pVALUE (no space) - match dash p (but not double dash) followed by non-dash, non-space characters
    text = re.sub(r'(?<!\-)(-p)(?!-)([^\s\-][^\s]*)', r'\1[REDACTED]', text, flags=re.IGNORECASE)

    # MongoDB patterns
    # MONGO_INITDB_ROOT_PASSWORD=... environment variable
    text = re.sub(r'(MONGO_INITDB_ROOT_PASSWORD)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)

    # PostgreSQL patterns
    # POSTGRES_PASSWORD=... or PGPASSWORD=... environment variable
    text = re.sub(r'((?:POSTGRES_)?PASSWORD)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    text = re.sub(r'(PGPASSWORD)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)

    # Generic patterns
    # DB_PASSWORD=... or similar (e.g., DB_USER_PASSWORD)
    text = re.sub(r'(DB[_\w]*PASSWORD)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    # password=... or passwd=...
    text = re.sub(r'(password|passwd)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)
    # secret=... or api_key=...
    text = re.sub(r'(secret|api_key|apikey|token)=([^\s\'\"]+)', r'\1=[REDACTED]', text, flags=re.IGNORECASE)

    # Authorization headers (e.g., "Authorization: Bearer ...")
    text = re.sub(r'(Authorization:\s+(?:Bearer|Basic)\s+)([^\s\'\"]+)', r'\1[REDACTED]', text, flags=re.IGNORECASE)

    return text
