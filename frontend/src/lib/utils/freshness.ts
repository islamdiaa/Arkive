/**
 * Helpers for the backup freshness watchdog (`backup_freshness` in /api/status).
 */
import type { BackupFreshness, StaleJob } from '$lib/types';

/** Format an hour count the way the backend notification text does ("15 h", "1.5 h"). */
export function formatHours(hours: number | null | undefined): string {
	if (typeof hours !== 'number' || !Number.isFinite(hours)) return 'its schedule';
	if (hours > 0 && hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
	return `${parseFloat(hours.toFixed(1))} h`;
}

function plural(n: number, unit: string): string {
	return `${n} ${unit}${n === 1 ? '' : 's'}`;
}

/** Long-form relative age: "just now", "5 minutes ago", "3 hours ago", "2 days ago". */
export function relativeAge(iso: string | null | undefined, now: Date = new Date()): string | null {
	if (!iso) return null;
	const then = new Date(iso);
	if (Number.isNaN(then.getTime())) return null;
	const seconds = Math.max(0, Math.floor((now.getTime() - then.getTime()) / 1000));
	if (seconds < 60) return 'just now';
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${plural(minutes, 'minute')} ago`;
	const hours = Math.floor(minutes / 60);
	if (hours < 48) return `${plural(hours, 'hour')} ago`;
	return `${plural(Math.floor(hours / 24), 'day')} ago`;
}

/**
 * One line per stale job, e.g.
 * "DB Dumps — last good backup 2 days ago (expected within 15 h)".
 */
export function formatStaleJobLine(job: StaleJob, now: Date = new Date()): string {
	const age = relativeAge(job.last_good_at, now) ?? 'never';
	return `${job.job_name} — last good backup ${age} (expected within ${formatHours(job.threshold_hours)})`;
}

/** Where a stale job links to: that job's run history on the Backups page. */
export function staleJobHref(job: Pick<StaleJob, 'job_id'>): string {
	return `/backups?job=${encodeURIComponent(job.job_id)}`;
}

/** Stale jobs from a status payload; empty unless the watchdog says stale. */
export function staleJobs(freshness: BackupFreshness | null | undefined): StaleJob[] {
	if (!freshness?.stale) return [];
	return Array.isArray(freshness.stale_jobs) ? freshness.stale_jobs : [];
}
