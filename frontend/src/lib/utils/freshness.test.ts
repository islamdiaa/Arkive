import { describe, expect, it } from 'vitest';

import { formatHours, formatStaleJobLine, relativeAge, staleJobHref, staleJobs } from './freshness';

const NOW = new Date('2026-10-22T20:00:00Z');

describe('formatHours', () => {
	it('formats whole and fractional hours like the backend notification text', () => {
		expect(formatHours(15)).toBe('15 h');
		expect(formatHours(15.0)).toBe('15 h');
		expect(formatHours(1.5)).toBe('1.5 h');
		expect(formatHours(30.25)).toBe('30.3 h');
		expect(formatHours(168)).toBe('168 h');
	});

	it('uses minutes below one hour', () => {
		expect(formatHours(0.5)).toBe('30 min');
	});

	it('falls back when the threshold is unknown', () => {
		expect(formatHours(null)).toBe('its schedule');
		expect(formatHours(undefined)).toBe('its schedule');
		expect(formatHours(Number.NaN)).toBe('its schedule');
	});
});

describe('relativeAge', () => {
	it('uses long-form units', () => {
		expect(relativeAge('2026-10-22T19:59:30Z', NOW)).toBe('just now');
		expect(relativeAge('2026-10-22T19:59:00Z', NOW)).toBe('1 minute ago');
		expect(relativeAge('2026-10-22T19:15:00Z', NOW)).toBe('45 minutes ago');
		expect(relativeAge('2026-10-22T17:00:00Z', NOW)).toBe('3 hours ago');
		expect(relativeAge('2026-10-21T00:00:00Z', NOW)).toBe('44 hours ago');
		expect(relativeAge('2026-10-20T18:00:05Z', NOW)).toBe('2 days ago');
	});

	it('returns null for missing or unparseable timestamps', () => {
		expect(relativeAge(null, NOW)).toBeNull();
		expect(relativeAge('', NOW)).toBeNull();
		expect(relativeAge('not a date', NOW)).toBeNull();
	});

	it('never reports a negative age for clock skew', () => {
		expect(relativeAge('2026-10-22T20:05:00Z', NOW)).toBe('just now');
	});
});

describe('formatStaleJobLine', () => {
	it('describes the last good backup and the expected window', () => {
		const line = formatStaleJobLine(
			{ job_id: 'a1', job_name: 'DB Dumps', last_good_at: '2026-10-20T18:00:05Z', threshold_hours: 15, overdue_hours: 21.5 },
			NOW,
		);
		expect(line).toBe('DB Dumps — last good backup 2 days ago (expected within 15 h)');
	});

	it('says "never" when the job has no good backup', () => {
		const line = formatStaleJobLine(
			{ job_id: 'a2', job_name: 'Full Backup', last_good_at: null, threshold_hours: 30, overdue_hours: 5 },
			NOW,
		);
		expect(line).toBe('Full Backup — last good backup never (expected within 30 h)');
	});
});

describe('staleJobHref', () => {
	it('links to the job run history on the Backups page', () => {
		expect(staleJobHref({ job_id: 'a1' })).toBe('/backups?job=a1');
		expect(staleJobHref({ job_id: 'job with/slash' })).toBe('/backups?job=job%20with%2Fslash');
	});
});

describe('staleJobs', () => {
	const job = { job_id: 'a1', job_name: 'DB Dumps', last_good_at: null, threshold_hours: 15, overdue_hours: 1 };

	it('returns the stale jobs only when the watchdog reports stale', () => {
		expect(staleJobs({ stale: true, checked_at: null, stale_jobs: [job] })).toEqual([job]);
		expect(staleJobs({ stale: false, checked_at: null, stale_jobs: [job] })).toEqual([]);
	});

	it('tolerates missing data', () => {
		expect(staleJobs(null)).toEqual([]);
		expect(staleJobs(undefined)).toEqual([]);
		expect(staleJobs({ stale: true, checked_at: null, stale_jobs: undefined as any })).toEqual([]);
	});
});
