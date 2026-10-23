/**
 * Backup job types and the copy used to explain them.
 *
 * Every job uploads a complete snapshot (dump folder + watched directories)
 * to the job's targets, or to all enabled targets when the job has none.
 * Types differ only in what is refreshed before the upload.
 */

export type JobType = 'full' | 'db_dump' | 'flash';

export const JOB_TYPES: JobType[] = ['full', 'db_dump', 'flash'];

export const JOB_TYPE_LABELS: Record<JobType, string> = {
	full: 'Full Backup',
	db_dump: 'Database Dumps',
	flash: 'Flash Drive',
};

export const JOB_TYPE_DESCRIPTIONS: Record<JobType, string> = {
	full: 'Re-dumps all databases and re-archives the Unraid flash drive, then uploads a complete snapshot.',
	db_dump: 'Re-dumps databases only, then uploads a complete snapshot (the flash archive is not refreshed).',
	flash: 'Re-archives the Unraid flash drive only, then uploads a complete snapshot (databases are not re-dumped).',
};

export const JOB_TYPES_OVERVIEW =
	'Every job uploads a complete snapshot of the dump folder and your watched directories to its targets ' +
	'(or to all enabled targets if none are selected). Types differ only in what is refreshed first.';

export function jobTypeLabel(type: string | null | undefined): string {
	if (!type) return 'Unknown';
	return JOB_TYPE_LABELS[type as JobType] ?? type;
}

export function jobTypeDescription(type: string | null | undefined): string {
	if (!type) return '';
	return JOB_TYPE_DESCRIPTIONS[type as JobType] ?? '';
}

/**
 * The job the dashboard's primary "Run Backup" button should trigger:
 * an enabled `full` job, then any `full` job, then the first job.
 */
export function pickPrimaryJob<T extends { type?: string; enabled?: boolean }>(jobs: T[] | null | undefined): T | null {
	if (!jobs || jobs.length === 0) return null;
	return (
		jobs.find((j) => j.type === 'full' && j.enabled !== false) ??
		jobs.find((j) => j.type === 'full') ??
		jobs[0]
	);
}
