"""Structured JSON logging configuration with sensitive value filtering."""

import logging
import logging.handlers
import re
import sys
from pathlib import Path

import structlog

# Patterns that indicate sensitive values in log messages.
# Matches compound names like secret_key=..., RESTIC_PASSWORD=..., api-key: ...
_SENSITIVE_PATTERNS = re.compile(
    r"(\w*(?:password|secret|token|api[_-]?key|authorization|credential)\w*)"
    r"\s*[=:]\s*(\S+)",
    re.IGNORECASE,
)

# Replacement for sensitive values
_REDACTION = r"\1=***REDACTED***"


class _SensitiveFilter(logging.Filter):
    """Filter that redacts sensitive values from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SENSITIVE_PATTERNS.sub(_REDACTION, record.msg)
        if record.args:
            new_args = []
            for arg in record.args if isinstance(record.args, tuple) else (record.args,):
                if isinstance(arg, str):
                    new_args.append(_SENSITIVE_PATTERNS.sub(_REDACTION, arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args) if isinstance(record.args, tuple) else new_args[0]
        return True


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Configure structured JSON logging to stdout and file.

    Uses stdlib logging for both console and file output.
    Configures structlog to route through stdlib for unified output.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "arkive.log"

    log_level = getattr(logging, level, logging.INFO)

    # Standard library logging -- single output path
    root = logging.getLogger()
    # Clear any pre-existing handlers to prevent duplicates on reload
    root.handlers.clear()
    root.setLevel(log_level)

    # Sensitive value filter applied to all handlers
    sensitive_filter = _SensitiveFilter()

    # JSON formatter for structured output
    fmt = logging.Formatter(
        '{"timestamp":"%(asctime)s","level":"%(levelname)s","component":"%(name)s","message":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # Console handler (stdout)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(fmt)
    console.addFilter(sensitive_filter)
    root.addHandler(console)

    # File handler with rotation (50 MB, 30 backups = max 1.5 GB log storage)
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=30)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(sensitive_filter)
    root.addHandler(file_handler)

    # Configure structlog to route through stdlib (prevents double-logging)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.EventRenamer("message"),
            # Route to stdlib instead of printing directly
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Suppress noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
