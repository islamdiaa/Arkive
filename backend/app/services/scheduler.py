"""APScheduler-based job scheduler — no cron dependency."""

import asyncio
import logging
import time

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.activity import log_activity
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.discovery_persistence import persist_discovery_results
from app.services.freshness import describe_stale_job, evaluate_freshness, ping_heartbeat
from app.services.lock_manager import BackupLockManager
from app.services.retention import read_retention_settings
from app.services.settings_store import DEFAULTS, get_bool_setting, get_setting
from app.utils.cron import crontab_trigger

logger = logging.getLogger("arkive.scheduler")

# System job IDs — not stored in backup_jobs table
SYSTEM_JOB_DISCOVERY = "system_discovery_scan"
SYSTEM_JOB_RETENTION = "system_retention_cleanup"
SYSTEM_JOB_HEALTH = "system_health_check"
SYSTEM_JOB_LOG_PRUNE = "system_activity_log_prune"
SYSTEM_JOB_INTEGRITY_CHECK = "system_integrity_check"
SYSTEM_JOB_INTEGRITY_VERIFY = "system_integrity_verify"
SYSTEM_JOB_FRESHNESS = "system_backup_freshness"
SYSTEM_JOB_RESTORE_DRILL = "system_restore_drill"

# Repeat the "backups overdue" alert this often while a job stays stale.
STALE_REMINDER_SECONDS = 24 * 3600

# Target status values written by the health check and the targets API.
_HEALTHY_STATES = {"online", "ok", "healthy"}


class ArkiveScheduler:
    """Manages scheduled backup jobs using APScheduler."""

    def __init__(
        self,
        orchestrator,
        config: ArkiveConfig,
        discovery=None,
        backup_engine=None,
        cloud_manager=None,
        notifier=None,
        verify_engine=None,
        event_bus=None,
        restore_drill=None,
    ):
        self.orchestrator = orchestrator
        self.config = config
        self.discovery = discovery
        self.backup_engine = backup_engine
        self.verify_engine = verify_engine
        self.event_bus = event_bus
        self.cloud_manager = cloud_manager
        self.notifier = notifier
        self.restore_drill = restore_drill
        self.scheduler = AsyncIOScheduler(
            job_defaults={
                "misfire_grace_time": 3600,
                "coalesce": True,
                "max_instances": 1,
            }
        )
        self._job_map: dict[str, str] = {}  # backup_job_id -> apscheduler_job_id
        self._background_tasks: set[asyncio.Task] = set()
        self._target_status: dict[str, str] = {}  # last health-check status per target
        self._stale_alerted: dict[str, float] = {}  # job_id -> monotonic time of last stale alert

    async def start(self) -> None:
        """Load all enabled jobs from DB, register system jobs, and start scheduler."""
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM backup_jobs WHERE enabled = 1")
            jobs = await cursor.fetchall()

        for job in jobs:
            self._add_job(dict(job))

        self._register_system_jobs()
        try:
            await self.reload_restore_drill_job()
        except Exception as e:
            logger.error("Could not schedule restore drills: %s", e)

        # Add error listener so job exceptions are always logged
        # APScheduler uses mask= parameter; EVENT_JOB_ERROR = 8192
        from apscheduler.events import EVENT_JOB_ERROR

        self.scheduler.add_listener(self._on_job_error, mask=EVENT_JOB_ERROR)

        self.scheduler.start()
        logger.info("Scheduler started with %d user jobs + system jobs", len(jobs))

    async def stop(self) -> None:
        """Gracefully stop the scheduler.

        APScheduler's AsyncIOScheduler.shutdown() is synchronous;
        wait=True lets in-flight jobs finish before stopping.
        """
        if self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=True)
                logger.info("Scheduler stopped gracefully")
            except Exception as e:
                logger.error("Error during scheduler shutdown: %s", e)
                try:
                    self.scheduler.shutdown(wait=False)
                    logger.warning("Scheduler force-stopped after error")
                except Exception:
                    pass

    @staticmethod
    def _on_job_error(event) -> None:
        """Listener for APScheduler job error events."""
        if hasattr(event, "exception") and event.exception:
            logger.error(
                "Scheduled job %s raised an exception: %s",
                event.job_id,
                event.exception,
            )
        else:
            logger.error("Scheduled job %s failed", event.job_id)

    # ---- System job registration ----

    def _register_system_jobs(self) -> None:
        """Register system jobs: discovery, retention, health check, integrity check, log prune.

        Idempotent: skips registration if the job already exists (handles both
        pre-start pending queue and running jobstore).
        """
        # 1. Discovery Scan — daily at 3 AM
        if not self.scheduler.get_job(SYSTEM_JOB_DISCOVERY):
            self.scheduler.add_job(
                self._run_discovery_scan,
                trigger=CronTrigger(hour=3, minute=0),
                id=SYSTEM_JOB_DISCOVERY,
                replace_existing=True,
                name="Discovery Scan",
            )
            logger.info("Registered system job: Discovery Scan (daily 3 AM)")

        # 2. Retention Cleanup — weekly Sunday at 4 AM
        if not self.scheduler.get_job(SYSTEM_JOB_RETENTION):
            self.scheduler.add_job(
                self._run_retention_cleanup,
                trigger=CronTrigger(day_of_week="sun", hour=4, minute=0),
                id=SYSTEM_JOB_RETENTION,
                replace_existing=True,
                name="Retention Cleanup",
            )
            logger.info("Registered system job: Retention Cleanup (weekly Sunday 4 AM)")

        # 3. Health Check — every 5 minutes
        if not self.scheduler.get_job(SYSTEM_JOB_HEALTH):
            self.scheduler.add_job(
                self._run_health_check,
                trigger=IntervalTrigger(minutes=5),
                id=SYSTEM_JOB_HEALTH,
                replace_existing=True,
                name="Health Check",
            )
            logger.info("Registered system job: Health Check (every 5m)")

        # 4. Activity Log Prune — daily at 2 AM
        if not self.scheduler.get_job(SYSTEM_JOB_LOG_PRUNE):
            self.scheduler.add_job(
                self._run_activity_log_prune,
                trigger=CronTrigger(hour=2, minute=0),
                id=SYSTEM_JOB_LOG_PRUNE,
                replace_existing=True,
                name="Activity Log Prune",
            )
            logger.info("Registered system job: Activity Log Prune (daily 2 AM)")

        # 5. Integrity Check — weekly Sunday at 5 AM (after retention at 4 AM)
        if not self.scheduler.get_job(SYSTEM_JOB_INTEGRITY_CHECK):
            self.scheduler.add_job(
                self._run_integrity_check,
                trigger=CronTrigger(day_of_week="sun", hour=5, minute=0),
                id=SYSTEM_JOB_INTEGRITY_CHECK,
                replace_existing=True,
                name="Integrity Check",
            )
            logger.info("Registered system job: Integrity Check (weekly Sunday 5 AM)")

        # 6. Integrity Verify — weekly Sunday at 6 AM (after integrity check at 5 AM)
        if not self.scheduler.get_job(SYSTEM_JOB_INTEGRITY_VERIFY):
            self.scheduler.add_job(
                self._run_integrity_verify,
                trigger=CronTrigger(day_of_week="sun", hour=6, minute=0),
                id=SYSTEM_JOB_INTEGRITY_VERIFY,
                replace_existing=True,
                name="Integrity Verify",
            )
            logger.info("Registered system job: Integrity Verify (weekly Sunday 6 AM)")

        # 7. Backup freshness + heartbeat — every 15 minutes
        if not self.scheduler.get_job(SYSTEM_JOB_FRESHNESS):
            self.scheduler.add_job(
                self._run_freshness_check,
                trigger=IntervalTrigger(minutes=15),
                id=SYSTEM_JOB_FRESHNESS,
                replace_existing=True,
                name="Backup Freshness",
            )
            logger.info("Registered system job: Backup Freshness (every 15m)")

    async def _run_freshness_check(self) -> None:
        """System job: alert on jobs without a recent good backup; send the heartbeat
        while everything is healthy.

        The heartbeat is a dead man's switch: an external monitor alerts when the
        pings stop, which covers Arkive itself being down as well as failing or
        overdue backups.
        """
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                freshness = await evaluate_freshness(db)
                heartbeat_url = (await get_setting(db, "heartbeat_url")).strip()
        except Exception as e:
            logger.error("System job: freshness check failed: %s", e)
            return

        now = time.monotonic()
        stale_ids = {job["job_id"] for job in freshness["stale_jobs"]}
        for job in freshness["stale_jobs"]:
            last_alert = self._stale_alerted.get(job["job_id"])
            if last_alert is not None and now - last_alert < STALE_REMINDER_SECONDS:
                continue
            first_alert = last_alert is None
            self._stale_alerted[job["job_id"]] = now
            message = describe_stale_job(job)
            logger.warning("Backup overdue: %s", message)
            if first_alert:
                try:
                    async with aiosqlite.connect(self.config.db_path) as db:
                        await log_activity(db, "backup", "stale", message, job, severity="warning")
                except Exception as e:
                    logger.warning("Could not record stale backup activity: %s", e)
            if self.notifier:
                try:
                    await self.notifier.send(
                        "backup.stale", "Backups Overdue", message, severity="error", job_id=job["job_id"]
                    )
                except Exception as e:
                    logger.warning("Failed to send stale backup notification: %s", e)

        for job_id in list(self._stale_alerted):
            if job_id in stale_ids:
                continue
            del self._stale_alerted[job_id]
            name = next((j["job_name"] for j in freshness["jobs"] if j["job_id"] == job_id), job_id)
            try:
                async with aiosqlite.connect(self.config.db_path) as db:
                    await log_activity(
                        db, "backup", "fresh", f"{name} is producing good backups again", {"job_id": job_id}
                    )
            except Exception as e:
                logger.warning("Could not record backup recovery activity: %s", e)

        if heartbeat_url and freshness["healthy"]:
            result = await ping_heartbeat(heartbeat_url)
            if result["ok"]:
                logger.debug("Heartbeat sent")
            else:
                logger.warning("Heartbeat ping failed: %s", result["error"])
        elif heartbeat_url:
            logger.info("Heartbeat withheld: backups need attention")

    async def _run_discovery_scan(self) -> None:
        """System job: run container discovery scan."""
        if not self.discovery:
            logger.debug("Discovery engine not available, skipping scan")
            return
        try:
            logger.info("System job: starting discovery scan")
            containers = await self.discovery.scan()
            count = len(containers) if containers else 0

            # Persist results to discovered_containers table
            async with aiosqlite.connect(self.config.db_path) as db:
                await persist_discovery_results(db, containers or [])
                await log_activity(
                    db,
                    "system",
                    "discovery_scan",
                    f"Discovery scan completed: {count} containers found",
                    {"container_count": count},
                )
            logger.info("System job: discovery scan complete — %d containers", count)
        except Exception as e:
            logger.error("System job: discovery scan failed: %s", e)

    async def _run_retention_cleanup(self) -> None:
        """System job: run retention policy (forget+prune) on all enabled targets.

        Reads keep_daily/keep_weekly/keep_monthly from the settings table,
        falling back to sane defaults if not configured.
        """
        if not self.backup_engine:
            logger.debug("Backup engine not available, skipping retention cleanup")
            return
        # Prune takes an exclusive restic lock and can run for a long time, so
        # hold the backup lock: scheduled backups queue behind it instead of
        # failing on "repository is already locked".
        lock_held = await self._acquire_backup_lock("system-retention")
        if not lock_held:
            logger.warning("System job: retention cleanup skipped — backup/restore still running")
            return
        try:
            logger.info("System job: starting retention cleanup")

            async with aiosqlite.connect(self.config.db_path) as db:
                keep_daily, keep_weekly, keep_monthly = await read_retention_settings(db)

            logger.info("Retention settings: daily=%d, weekly=%d, monthly=%d", keep_daily, keep_weekly, keep_monthly)

            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                targets = [dict(row) for row in await cursor.fetchall()]

            if not targets:
                logger.info("System job: no enabled targets, skipping retention")
                return

            results = []
            for target in targets:
                try:
                    # Decrypt the config before passing to backup engine
                    target["config"] = decrypt_config(
                        target.get("config", "{}"),
                        str(self.config.config_dir),
                    )
                    # Merge snapshot groups split by old container-ID host names
                    # first, or retention never thins them out.
                    try:
                        relabel = await self.backup_engine.normalize_legacy_hostnames(
                            target, str(self.config.dump_dir), await self._owned_snapshot_ids(target["id"])
                        )
                        if isinstance(relabel, dict) and relabel.get("status") == "failed":
                            logger.warning(
                                "Could not re-label legacy snapshots on target %s: %s",
                                target["id"],
                                relabel.get("error"),
                            )
                    except Exception as e:
                        logger.warning("Legacy snapshot re-label skipped for target %s: %s", target["id"], e)
                    result = await self.backup_engine.forget(
                        target,
                        keep_daily=keep_daily,
                        keep_weekly=keep_weekly,
                        keep_monthly=keep_monthly,
                    )
                    results.append(
                        {
                            "target_id": target["id"],
                            "status": result.get("status", "unknown"),
                        }
                    )
                    logger.info(
                        "Retention cleanup for target %s: %s",
                        target["id"],
                        result.get("status"),
                    )
                except Exception as e:
                    logger.error("Retention cleanup failed for target %s: %s", target["id"], e)
                    results.append(
                        {
                            "target_id": target["id"],
                            "status": "failed",
                            "error": str(e),
                        }
                    )

            async with aiosqlite.connect(self.config.db_path) as db:
                await log_activity(
                    db,
                    "system",
                    "retention_cleanup",
                    f"Retention cleanup completed for {len(targets)} targets",
                    {"results": results},
                )
            logger.info("System job: retention cleanup complete — %d targets processed", len(targets))
        except Exception as e:
            logger.error("System job: retention cleanup failed: %s", e)
        finally:
            self._release_backup_lock()

    async def _owned_snapshot_ids(self, target_id: str) -> set[str]:
        """Snapshot IDs this instance's own backup runs recorded for a target."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute(
                    "SELECT DISTINCT snapshot_id FROM job_run_targets "
                    "WHERE target_id = ? AND snapshot_id IS NOT NULL AND snapshot_id != ''",
                    (target_id,),
                )
                return {row[0] for row in await cursor.fetchall()}
        except Exception as e:
            logger.warning("Could not read snapshot history for target %s: %s", target_id, e)
            return set()

    async def _run_health_check(self) -> None:
        """System job: test connectivity for all enabled storage targets."""
        if not self.cloud_manager:
            logger.debug("Cloud manager not available, skipping health check")
            return
        try:
            logger.debug("System job: starting health check")
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                targets = [dict(row) for row in await cursor.fetchall()]

            if not targets:
                logger.debug("System job: no enabled targets, skipping health check")
                return

            changes: list[dict] = []
            for target in targets:
                previous_status = self._target_status.get(target["id"], target.get("status"))
                try:
                    # Decrypt the config for the connectivity test
                    target["config"] = decrypt_config(
                        target.get("config", "{}"),
                        str(self.config.config_dir),
                    )
                    result = await self.cloud_manager.test_target(target)
                    new_status = "online" if result.get("status") == "ok" else "error"

                    async with aiosqlite.connect(self.config.db_path) as db:
                        await db.execute(
                            """UPDATE storage_targets
                               SET status = ?, last_tested = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                               WHERE id = ?""",
                            (new_status, target["id"]),
                        )
                        await db.commit()
                    logger.debug(
                        "Health check for target %s (%s): %s",
                        target["id"],
                        target.get("name"),
                        new_status,
                    )
                except Exception as e:
                    new_status = "error"
                    logger.error("Health check failed for target %s: %s", target["id"], e)
                    # Mark as error on exception
                    try:
                        async with aiosqlite.connect(self.config.db_path) as db:
                            await db.execute(
                                """UPDATE storage_targets
                                   SET status = 'error', last_tested = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                                       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                                   WHERE id = ?""",
                                (target["id"],),
                            )
                            await db.commit()
                    except Exception as db_err:
                        logger.debug("Could not record error status for target %s: %s", target["id"], db_err)

                self._target_status[target["id"]] = new_status
                was_healthy = previous_status in _HEALTHY_STATES
                is_healthy = new_status in _HEALTHY_STATES
                first_check = previous_status in (None, "", "unknown")
                if was_healthy != is_healthy and not (first_check and is_healthy):
                    changes.append(
                        {
                            "target_id": target["id"],
                            "name": target.get("name"),
                            "from": previous_status,
                            "to": new_status,
                        }
                    )

            # Routine checks run every 5 minutes; only status changes are
            # worth an activity entry (and a notification when a target fails).
            for change in changes:
                went_bad = change["to"] == "error"
                message = f"Storage target {change['name']} is {'unreachable' if went_bad else 'reachable again'}"
                if went_bad:
                    logger.warning(message)
                else:
                    logger.info(message)
                async with aiosqlite.connect(self.config.db_path) as db:
                    await log_activity(
                        db,
                        "target",
                        "health_changed",
                        message,
                        change,
                        severity="warning" if went_bad else "info",
                    )
                if went_bad and self.notifier:
                    try:
                        await self.notifier.send(
                            "target.error", "Storage Target Unreachable", message, severity="error"
                        )
                    except Exception as notify_err:
                        logger.warning("Failed to send target health notification: %s", notify_err)
            logger.debug("System job: health check complete — %d targets checked", len(targets))
        except Exception as e:
            logger.error("System job: health check failed: %s", e)

    async def _run_activity_log_prune(self) -> None:
        """System job: delete activity_log entries older than 90 days."""
        try:
            logger.info("System job: starting activity log prune")
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute(
                    "DELETE FROM activity_log WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-90 days')"
                )
                deleted = cursor.rowcount
                await db.commit()
                await log_activity(
                    db,
                    "system",
                    "activity_log_prune",
                    f"Activity log pruned: {deleted} entries older than 90 days removed",
                    {"deleted_count": deleted},
                )
            logger.info("System job: activity log prune complete — %d entries removed", deleted)
        except Exception as e:
            logger.error("System job: activity log prune failed: %s", e)

    async def _run_integrity_check(self) -> None:
        """System job: run restic repository integrity check on all enabled targets.

        Calls BackupEngine.check() for each enabled storage target. On failure,
        sends a notification via Notifier if available, and always logs an
        activity entry for each result.
        """
        if not self.backup_engine:
            logger.debug("Backup engine not available, skipping integrity check")
            return
        try:
            logger.info("System job: starting integrity check")

            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                targets = [dict(row) for row in await cursor.fetchall()]

            if not targets:
                logger.info("System job: no enabled targets, skipping integrity check")
                return

            results = []
            for target in targets:
                target_name = target.get("name", target["id"])
                try:
                    target["config"] = decrypt_config(
                        target.get("config", "{}"),
                        str(self.config.config_dir),
                    )
                    result = await self.backup_engine.check(target)
                    results.append(
                        {
                            "target_id": target["id"],
                            "status": result.get("status", "unknown"),
                        }
                    )
                    logger.info(
                        "Integrity check for target %s (%s): %s",
                        target["id"],
                        target_name,
                        result.get("status"),
                    )
                except Exception as e:
                    error_msg = str(e)
                    logger.error(
                        "Integrity check failed for target %s (%s): %s",
                        target["id"],
                        target_name,
                        error_msg,
                    )
                    results.append(
                        {
                            "target_id": target["id"],
                            "status": "failed",
                            "error": error_msg,
                        }
                    )
                    if self.notifier:
                        try:
                            await self.notifier.send(
                                "verification.failed",
                                "Integrity Check Failed",
                                f"Repository {target_name} failed integrity check: {error_msg}",
                                "error",
                            )
                        except Exception as notify_err:
                            logger.error("Failed to send integrity failure notification: %s", notify_err)

            async with aiosqlite.connect(self.config.db_path) as db:
                await log_activity(
                    db,
                    "system",
                    "integrity_check",
                    f"Integrity check completed for {len(targets)} targets",
                    {"results": results},
                )
            logger.info("System job: integrity check complete — %d targets processed", len(targets))
        except Exception as e:
            logger.error("System job: integrity check failed: %s", e)

    async def _run_integrity_verify(self) -> None:
        """System job: run full verification pipeline (restic check + restore test + DB validation).

        Checks the backup lock before starting to avoid conflicts with active backups.
        Delegates to VerifyEngine for the actual verification work.
        """
        if not self.verify_engine:
            logger.debug("Verify engine not available, skipping integrity verify")
            return
        try:
            if not await self._wait_until_idle(self.system_job_lock_wait_seconds):
                logger.warning("System job: integrity verify skipped — backup/restore still running")
                return
            logger.info("System job: starting integrity verify")
            result = await self.verify_engine.verify_all_targets()
            status = result.get("status", "unknown")
            trust_score = result.get("overall_trust_score", 0.0)

            async with aiosqlite.connect(self.config.db_path) as db:
                severity = "info" if status == "completed" else "warning"
                await log_activity(
                    db,
                    "system",
                    "integrity_verify",
                    f"Integrity verify {status}: trust score {trust_score}",
                    {"status": status, "trust_score": trust_score, "targets": result.get("targets", [])},
                    severity=severity,
                )

            # Publish SSE event so the frontend updates
            if self.event_bus:
                await self.event_bus.publish(
                    "verification:completed",
                    {
                        "status": status,
                        "trust_score": trust_score,
                        "target_count": len(result.get("targets", [])),
                    },
                )

            # Notify on failure or low trust score
            failed_targets = [
                t for t in result.get("targets", []) if t.get("status") == "failed" or t.get("trust_score", 100) < 50
            ]
            if failed_targets and self.notifier:
                names = ", ".join(t.get("target_name", t.get("target_id", "?")) for t in failed_targets)
                try:
                    await self.notifier.send(
                        "verification.failed",
                        "Verification Failed",
                        f"Verification failed for: {names}. Trust score: {trust_score}",
                        "error",
                    )
                except Exception as notify_err:
                    logger.warning("Failed to send verification failure notification: %s", notify_err)

            logger.info(
                "System job: integrity verify complete — status=%s trust_score=%.1f",
                status,
                trust_score,
            )
        except Exception as e:
            logger.error("System job: integrity verify failed: %s", e)

    # ---- Restore drills ----

    async def reload_restore_drill_job(self) -> None:
        """(Re-)register the restore drill system job from the current settings.

        Called at start and by the settings API when restore_drill_enabled or
        restore_drill_schedule change: adds, replaces or removes the job.
        """
        async with aiosqlite.connect(self.config.db_path) as db:
            enabled = await get_bool_setting(db, "restore_drill_enabled")
            schedule = (await get_setting(db, "restore_drill_schedule")).strip()

        if not enabled or self.restore_drill is None:
            if self.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL):
                self.scheduler.remove_job(SYSTEM_JOB_RESTORE_DRILL)
                logger.info("Removed system job: Restore Drill")
            return
        try:
            trigger = crontab_trigger(schedule)
        except ValueError:
            logger.error(
                "Invalid restore_drill_schedule %r; using default %r", schedule, DEFAULTS["restore_drill_schedule"]
            )
            schedule = DEFAULTS["restore_drill_schedule"]
            trigger = crontab_trigger(schedule)
        # Remove first: before start() replace_existing doesn't dedupe the pending queue.
        if self.scheduler.get_job(SYSTEM_JOB_RESTORE_DRILL):
            self.scheduler.remove_job(SYSTEM_JOB_RESTORE_DRILL)
        self.scheduler.add_job(
            self._run_restore_drill,
            trigger=trigger,
            id=SYSTEM_JOB_RESTORE_DRILL,
            replace_existing=True,
            name="Restore Drill",
        )
        logger.info("Registered system job: Restore Drill (%s)", schedule)

    async def _run_restore_drill(self) -> None:
        """System job: restore database dumps from a snapshot and load them.

        The engine waits for running backups/restores itself and never raises
        into the scheduler.
        """
        if not self.restore_drill:
            return
        try:
            await self.restore_drill.run_drill(trigger="scheduled")
        except Exception as e:
            logger.error("System job: restore drill failed: %s", e)
        await self._alert_on_untested_drills()

    # Consecutive drills that tested nothing before it's reported as a failure.
    drill_skip_alert_threshold = 3

    async def _alert_on_untested_drills(self) -> None:
        """A drill that can't start a test server is "skipped", not failed — but
        drills that never test anything mean restores are unproven. Alert once
        the last N drills all tested nothing."""
        try:
            async with aiosqlite.connect(self.config.db_path) as db:
                cursor = await db.execute(
                    "SELECT status, error_message FROM restore_drills "
                    "WHERE status != 'running' ORDER BY started_at DESC LIMIT ?",
                    (self.drill_skip_alert_threshold,),
                )
                recent = await cursor.fetchall()
        except Exception as e:
            logger.debug("Could not read restore drill history: %s", e)
            return
        if len(recent) < self.drill_skip_alert_threshold or any(r[0] != "skipped" for r in recent):
            return
        message = (
            f"The last {len(recent)} restore drills could not test any database: "
            f"{(recent[0][1] or 'no reason recorded')[:300]}"
        )
        logger.warning(message)
        if self.notifier:
            try:
                await self.notifier.send(
                    "drill.failed",
                    "Restore Drills Not Testing Anything",
                    message,
                    severity="warning",
                    dedupe_key="drill-untested",
                )
            except Exception as e:
                logger.warning("Failed to send untested-drill notification: %s", e)

    # ---- Backup lock coordination for system jobs ----

    # How long weekly maintenance waits for a running backup before skipping.
    system_job_lock_wait_seconds: float = 3 * 3600
    lock_poll_seconds: float = 15

    def _lock_manager(self) -> BackupLockManager | None:
        lock_manager = getattr(self.orchestrator, "lock_manager", None)
        return lock_manager if isinstance(lock_manager, BackupLockManager) else None

    async def _acquire_backup_lock(self, holder: str) -> bool:
        lock_manager = self._lock_manager()
        if lock_manager is None:
            return True
        deadline = time.monotonic() + self.system_job_lock_wait_seconds
        while True:
            if lock_manager.acquire_backup_lock(holder):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.lock_poll_seconds)

    def _release_backup_lock(self) -> None:
        lock_manager = self._lock_manager()
        if lock_manager is not None:
            lock_manager.release_backup_lock()

    async def _wait_until_idle(self, timeout: float) -> bool:
        """Wait until no backup or restore holds the lock."""
        lock_manager = self._lock_manager()
        if lock_manager is None:
            return True
        deadline = time.monotonic() + timeout
        while True:
            lock_manager.cleanup_stale_backup_lock()
            if not lock_manager.is_backup_running() and not lock_manager.is_restore_running():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.lock_poll_seconds)

    # ---- User backup job management ----

    def _add_job(self, job: dict) -> None:
        """Add a single backup job to the scheduler."""
        job_id = job["id"]
        schedule = job["schedule"]
        try:
            trigger = crontab_trigger(schedule)
            apscheduler_id = f"backup_{job_id}"
            self.scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=apscheduler_id,
                args=[job_id, job.get("type", "full")],
                replace_existing=True,
                name=job.get("name", job_id),
            )
            self._job_map[job_id] = apscheduler_id
            logger.info("Scheduled job %s: %s (%s)", job_id, job.get("name"), schedule)
        except Exception as e:
            logger.error("Failed to schedule job %s: %s", job_id, e)

    async def _run_job(self, job_id: str, job_type: str) -> None:
        """Execute a scheduled backup job."""
        logger.info("Scheduler triggering job %s (type=%s)", job_id, job_type)
        try:
            await self.orchestrator.run_backup(job_id=job_id, trigger="scheduled")
        except Exception as e:
            logger.error("Scheduled job %s failed: %s", job_id, e)

    async def reschedule_job(self, job_id: str, schedule: str) -> None:
        """Update a job's schedule."""
        apscheduler_id = self._job_map.get(job_id)
        if apscheduler_id and self.scheduler.get_job(apscheduler_id):
            trigger = crontab_trigger(schedule)
            self.scheduler.reschedule_job(apscheduler_id, trigger=trigger)
            logger.info("Rescheduled job %s to %s", job_id, schedule)
        else:
            # Job not in scheduler, add it
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
                job = await cursor.fetchone()
                if job:
                    self._add_job(dict(job))

    async def remove_job(self, job_id: str) -> None:
        """Remove a job from the scheduler."""
        apscheduler_id = self._job_map.pop(job_id, None)
        if apscheduler_id and self.scheduler.get_job(apscheduler_id):
            self.scheduler.remove_job(apscheduler_id)
            logger.info("Removed job %s from scheduler", job_id)

    async def pause_job(self, job_id: str) -> None:
        """Pause a scheduled job."""
        apscheduler_id = self._job_map.get(job_id)
        if apscheduler_id:
            self.scheduler.pause_job(apscheduler_id)

    async def resume_job(self, job_id: str) -> None:
        """Resume a paused job."""
        apscheduler_id = self._job_map.get(job_id)
        if apscheduler_id:
            self.scheduler.resume_job(apscheduler_id)

    def get_next_run(self, job_id: str) -> str | None:
        """Get next scheduled run time for a job."""
        apscheduler_id = self._job_map.get(job_id)
        if apscheduler_id:
            job = self.scheduler.get_job(apscheduler_id)
            nrt = getattr(job, "next_run_time", None) if job else None
            if nrt:
                return nrt.isoformat()
        return None

    def get_all_next_runs(self) -> dict[str, str]:
        """Get next run times for all jobs."""
        result = {}
        for job_id, apscheduler_id in self._job_map.items():
            job = self.scheduler.get_job(apscheduler_id)
            nrt = getattr(job, "next_run_time", None) if job else None
            if nrt:
                result[job_id] = nrt.isoformat()
        return result

    async def trigger_job(self, job_id: str) -> None:
        """Start a job now in the background (used by setup's first backup)."""
        if job_id not in self._job_map:
            return

        async def _run() -> None:
            try:
                await self.orchestrator.run_backup(job_id=job_id, trigger="manual")
            except Exception as e:
                logger.error("Triggered job %s failed: %s", job_id, e)

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def add_job(self, job: dict) -> None:
        """Register a new job with the scheduler."""
        self._add_job(job)
