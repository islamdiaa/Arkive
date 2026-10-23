"""Correlation ID context for structured logging.

Provides a ContextVar that holds the current backup run_id and a logging
filter that injects it into every LogRecord so all log lines emitted
during a backup run share the same correlation identifier.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

# Holds the run_id for the current async task / coroutine chain.
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


class CorrelationFilter(logging.Filter):
    """Inject ``run_id`` from the current ContextVar into each LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = current_run_id.get() or "-"  # type: ignore[attr-defined]
        return True
