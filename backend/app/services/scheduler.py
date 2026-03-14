"""APScheduler-based job scheduler — no cron dependency."""

import logging

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.activity import log_activity
from app.core.config import ArkiveConfig
from app.core.security import decrypt_config
from app.services.discovery_persistence import persist_discovery_results

logger = logging.getLogger("arkive.scheduler")

# System job IDs — not stored in backup_jobs table
SYSTEM_JOB_DISCOVERY = "system_discovery_scan"
SYSTEM_JOB_RETENTION = "system_retention_cleanup"
SYSTEM_JOB_HEALTH = "system_health_check"
SYSTEM_JOB_LOG_PRUNE = "system_activity_log_prune"
SYSTEM_JOB_INTEGRITY_CHECK = "system_integrity_check"


class ArkiveScheduler:
    """Manages scheduled backup jobs using APScheduler."""

    def __init__(
        self, orchestrator, config: ArkiveConfig, discovery=None, backup_engine=None, cloud_manager=None, notifier=None
    ):
        self.orchestrator = orchestrator
        self.config = config
        self.discovery = discovery
        self.backup_engine = backup_engine
        self.cloud_manager = cloud_manager
        self.notifier = notifier
        self.scheduler = AsyncIOScheduler(
            job_defaults={
                "misfire_grace_time": 3600,
                "coalesce": True,
                "max_instances": 1,
            }
        )
        self._job_map: dict[str, str] = {}  # backup_job_id -> apscheduler_job_id

    async def start(self) -> None:
        """Load all enabled jobs from DB, register system jobs, and start scheduler."""
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM backup_jobs WHERE enabled = 1")
            jobs = await cursor.fetchall()

        for job in jobs:
            self._add_job(dict(job))

        self._register_system_jobs()

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
                await db.commit()
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
        try:
            logger.info("System job: starting retention cleanup")

            # Read retention settings from DB (same logic as orchestrator)
            keep_daily = 7
            keep_weekly = 4
            keep_monthly = 6
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('keep_daily', 'keep_weekly', 'keep_monthly')"
                )
                for row in await cursor.fetchall():
                    try:
                        val = int(row["value"])
                        if val >= 1:  # Prevent accidental deletion of all snapshots
                            if row["key"] == "keep_daily":
                                keep_daily = val
                            elif row["key"] == "keep_weekly":
                                keep_weekly = val
                            elif row["key"] == "keep_monthly":
                                keep_monthly = val
                    except (ValueError, TypeError):
                        pass

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

    async def _run_health_check(self) -> None:
        """System job: test connectivity for all enabled storage targets."""
        if not self.cloud_manager:
            logger.debug("Cloud manager not available, skipping health check")
            return
        try:
            logger.info("System job: starting health check")
            async with aiosqlite.connect(self.config.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
                targets = [dict(row) for row in await cursor.fetchall()]

            if not targets:
                logger.info("System job: no enabled targets, skipping health check")
                return

            for target in targets:
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
                    logger.info(
                        "Health check for target %s (%s): %s",
                        target["id"],
                        target.get("name"),
                        new_status,
                    )
                except Exception as e:
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
                    except Exception:
                        logger.warning("Failed to update target %s status to error", target["id"], exc_info=True)

            async with aiosqlite.connect(self.config.db_path) as db:
                await log_activity(
                    db,
                    "system",
                    "health_check",
                    f"Health check completed for {len(targets)} targets",
                    {"target_count": len(targets)},
                )
            logger.info("System job: health check complete — %d targets checked", len(targets))
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
                                "integrity.failed",
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

    # ---- User backup job management ----

    def _add_job(self, job: dict) -> None:
        """Add a single backup job to the scheduler."""
        job_id = job["id"]
        schedule = job["schedule"]
        try:
            trigger = CronTrigger.from_crontab(schedule)
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
            trigger = CronTrigger.from_crontab(schedule)
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
        """Immediately trigger a scheduled job."""
        if job_id in self._job_map:
            await self._run_job(job_id, "full")

    async def add_job(self, job: dict) -> None:
        """Register a new job with the scheduler."""
        self._add_job(job)
