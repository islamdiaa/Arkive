"""Cron expression validation utility."""

from croniter import croniter
from fastapi import HTTPException


def validate_cron_expression(expression: str) -> str:
    """Validate a cron expression and return the cleaned version.

    Raises HTTPException(422) if the expression is invalid.
    """
    stripped = expression.strip()
    parts = stripped.split()
    if len(parts) != 5:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid cron expression: expected 5 fields, got {len(parts)}",
        )
    try:
        croniter(stripped)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {exc}")
    return stripped
