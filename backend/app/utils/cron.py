"""Cron expressions -> APScheduler triggers, with cron's day-of-week numbering.

APScheduler 3 numbers weekdays from Monday = 0, and ``CronTrigger.from_crontab``
passes the day-of-week field through unchanged, so ``"0 7 * * 0"`` fires on
Mondays and ``"0 2 * * 1-5"`` on Tuesday-Saturday. Everything else in Arkive
(the UI, cron previews, the freshness watchdog) reads expressions the standard
way, with 0 or 7 = Sunday. Always build triggers through ``crontab_trigger``.
"""

from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def crontab_trigger(expr: str, timezone=None) -> CronTrigger:
    """A CronTrigger for a 5-field crontab expression with standard weekday numbers.

    Raises ValueError for an invalid expression or one APScheduler can't express
    (the ``#`` nth-weekday syntax). Note: when both day-of-month and day-of-week
    are restricted, cron fires when *either* matches but APScheduler requires
    both; Arkive's schedules never combine the two.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_day_of_week(day_of_week),
        timezone=timezone,
    )


def _day_of_week(field: str) -> str:
    if field in ("*", "?"):
        return "*"
    try:
        expanded, nth_weekday = croniter.expand(f"* * * * {field}")
    except Exception as exc:  # croniter raises several error types
        raise ValueError(f"Invalid day-of-week field {field!r}: {exc}") from exc
    if nth_weekday:
        raise ValueError(f"Unsupported day-of-week field {field!r} (nth-weekday syntax)")
    days = expanded[4]
    if days == ["*"]:
        return "*"
    return ",".join(_DAY_NAMES[int(d) % 7] for d in sorted({int(d) % 7 for d in days}))
