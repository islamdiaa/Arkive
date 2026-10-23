"""Catalog of notification events Arkive emits.

The backend sends exactly these event types and the UI offers exactly these as
channel subscriptions — keep both sides on this list (served by
``GET /api/notifications/events``) so a subscription can never silently miss.
"""

NOTIFICATION_EVENTS: list[dict[str, str]] = [
    {
        "value": "backup.success",
        "label": "Backup succeeded",
        "description": "A backup completed with every phase successful",
    },
    {
        "value": "backup.partial",
        "label": "Backup incomplete",
        "description": "A snapshot was written but something was missed (a database, a path, a target)",
    },
    {"value": "backup.failed", "label": "Backup failed", "description": "A backup run failed"},
    {
        "value": "backup.stale",
        "label": "Backups overdue",
        "description": "A job has not produced a good backup within its schedule window",
    },
    {
        "value": "verification.failed",
        "label": "Verification failed",
        "description": "A repository integrity check or restore test failed",
    },
    {
        "value": "drill.failed",
        "label": "Restore drill failed",
        "description": "A database dump could not be restored into a test instance",
    },
    {
        "value": "target.error",
        "label": "Storage target unreachable",
        "description": "A storage target stopped responding to health checks",
    },
    {"value": "system.startup", "label": "Arkive started", "description": "The Arkive container started"},
    {"value": "system.shutdown", "label": "Arkive stopped", "description": "The Arkive container is shutting down"},
]

EVENT_TYPES = frozenset(e["value"] for e in NOTIFICATION_EVENTS)

# Subscriptions for a new channel when the caller doesn't pick: everything that
# means "your backups need attention".
DEFAULT_EVENTS: list[str] = [
    "backup.failed",
    "backup.partial",
    "backup.stale",
    "verification.failed",
    "drill.failed",
    "target.error",
]
