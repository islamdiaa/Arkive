"""Credential redaction utilities for secure logging.

Redacts sensitive values from log output and error messages to prevent
credential exposure when database commands fail or subprocess operations error.
"""

import re

_REDACTED = "[REDACTED]"

# Flags whose *next* argv element is a secret (``--password hunter2``).
_SECRET_VALUE_FLAGS = {
    "--password",
    "--pass",
    "--passwd",
    "--secret",
    "--token",
    "--api-key",
    "--apikey",
    "--key",
    "--access-key",
    "--secret-key",
    "--client-secret",
    "--password-command",
    "--sftp-pass",
    "--b2-key",
    "--s3-secret-access-key",
    "--s3-access-key-id",
    "-p",
}

# ``name=value`` argv elements (or env-style assignments) whose value is secret.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"^(-{0,2}[\w.-]*(?:password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|app[_-]?key|credential)[\w.-]*=)(.+)$",
    re.IGNORECASE,
)

# Commands whose positional arguments are themselves secrets.
_SECRET_POSITIONAL_SUBCOMMANDS = {("rclone", "obscure"), ("rclone", "reveal")}


def redact_command(cmd: list[str]) -> str:
    """Render an argv list for logging with secret-bearing arguments masked.

    Masks the value following flags like ``--password``/``-p``, the value part
    of ``password=...``/``pass=...``/``token=...`` style arguments, MySQL-style
    ``-pSECRET`` and every positional argument of ``rclone obscure``/``reveal``.
    The remaining text is additionally passed through ``redact_credentials``.
    """
    parts: list[str] = []
    redact_next = False
    redact_positionals = False
    base = [str(c) for c in cmd]
    names = [b.rsplit("/", 1)[-1] for b in base[:2]]
    if len(names) == 2 and (names[0], names[1]) in _SECRET_POSITIONAL_SUBCOMMANDS:
        redact_positionals = True

    for index, arg in enumerate(base):
        if redact_next:
            parts.append(_REDACTED)
            redact_next = False
            continue
        if redact_positionals and index >= 2 and arg not in ("-", "--") and not arg.startswith("-"):
            parts.append(_REDACTED)
            continue
        if redact_positionals and index >= 2 and arg == "--":
            parts.append(arg)
            continue
        lowered = arg.lower()
        if lowered in _SECRET_VALUE_FLAGS:
            parts.append(arg)
            redact_next = True
            continue
        match = _SECRET_ASSIGNMENT_RE.match(arg)
        if match:
            parts.append(f"{match.group(1)}{_REDACTED}")
            continue
        # MySQL/MariaDB short form: -pSECRET (but not -P port).
        if arg.startswith("-p") and len(arg) > 2 and not arg.startswith("--"):
            parts.append(f"-p{_REDACTED}")
            continue
        parts.append(arg)

    return redact_credentials(" ".join(parts))


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
    text = re.sub(r"(MYSQL_P(?:WD|ASSWORD))=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    # MARIADB_PASSWORD=... environment variable
    text = re.sub(r"(MARIADB_PASSWORD)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    # --password=value command-line arg (must be before -p pattern to avoid partial match)
    text = re.sub(r"(--password=)([^\s\'\"]+)", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    # --password value command-line arg (space-separated) (must be before -p pattern)
    text = re.sub(r"(--password)\s+([^\-\s][^\s]*)", r"\1 [REDACTED]", text, flags=re.IGNORECASE)
    # -pVALUE (no space) — MySQL/MariaDB short password flag.
    # Case-sensitive: matches -p (password) but NOT -P (port).
    # (?!-) prevents matching --password (already handled above).
    # False positives on rare flags like -print are acceptable (overly cautious, not a leak).
    text = re.sub(r"(?<!\-)(-p)(?!-)([^\s\-][^\s]*)", r"\1[REDACTED]", text)

    # MongoDB patterns
    # MONGO_INITDB_ROOT_PASSWORD=... environment variable
    text = re.sub(r"(MONGO_INITDB_ROOT_PASSWORD)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)

    # PostgreSQL patterns
    # POSTGRES_PASSWORD=... or PGPASSWORD=... environment variable
    text = re.sub(r"((?:POSTGRES_)?PASSWORD)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(PGPASSWORD)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)

    # Generic patterns
    # DB_PASSWORD=... or similar (e.g., DB_USER_PASSWORD)
    text = re.sub(r"(DB[_\w]*PASSWORD)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    # password=... or passwd=...
    text = re.sub(r"(password|passwd)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    # secret=... or api_key=...
    text = re.sub(r"(secret|api_key|apikey|token)=([^\s\'\"]+)", r"\1=[REDACTED]", text, flags=re.IGNORECASE)

    # Authorization headers (e.g., "Authorization: Bearer ...")
    text = re.sub(r"(Authorization:\s+(?:Bearer|Basic)\s+)([^\s\'\"]+)", r"\1[REDACTED]", text, flags=re.IGNORECASE)

    return text
