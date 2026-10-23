import { describe, expect, it } from 'vitest';

import { jobTypeDescription, jobTypeLabel, pickPrimaryJob } from './job-types';
import { phaseIndexIn, phasesForJobType } from '$lib/constants/backup-phases';

describe('jobTypeLabel', () => {
	it('maps known types to friendly labels and passes unknown values through', () => {
		expect(jobTypeLabel('full')).toBe('Full Backup');
		expect(jobTypeLabel('db_dump')).toBe('Database Dumps');
		expect(jobTypeLabel('flash')).toBe('Flash Drive');
		expect(jobTypeLabel('other')).toBe('other');
		expect(jobTypeLabel(undefined)).toBe('Unknown');
	});

	it('has a description for every known type', () => {
		expect(jobTypeDescription('full')).toMatch(/databases/);
		expect(jobTypeDescription('db_dump')).toMatch(/databases only/);
		expect(jobTypeDescription('flash')).toMatch(/flash drive only/);
	});
});

describe('pickPrimaryJob', () => {
	it('prefers an enabled full job over the first job in the list', () => {
		const jobs = [
			{ id: 'db', type: 'db_dump', enabled: true },
			{ id: 'full-off', type: 'full', enabled: false },
			{ id: 'full', type: 'full', enabled: true },
		];
		expect(pickPrimaryJob(jobs)?.id).toBe('full');
	});

	it('falls back to a disabled full job, then to the first job', () => {
		expect(pickPrimaryJob([{ id: 'db', type: 'db_dump' }, { id: 'f', type: 'full', enabled: false }])?.id).toBe('f');
		expect(pickPrimaryJob([{ id: 'db', type: 'db_dump' }, { id: 'fl', type: 'flash' }])?.id).toBe('db');
		expect(pickPrimaryJob([])).toBeNull();
	});
});

describe('phasesForJobType', () => {
	it('omits phases the job type does not run', () => {
		expect(phasesForJobType('db_dump').map((p) => p.key)).not.toContain('flash_backup');
		expect(phasesForJobType('flash').map((p) => p.key)).not.toContain('dumping_databases');
		expect(phasesForJobType('full')).toHaveLength(6);
		expect(phasesForJobType(null)).toHaveLength(6);
	});

	it('maps backend phase strings onto the filtered list', () => {
		const dbPhases = phasesForJobType('db_dump');
		expect(phaseIndexIn(dbPhases, 'uploading:b2')).toBe(dbPhases.findIndex((p) => p.key === 'uploading'));
		// A phase missing from the list lands on the next phase that exists.
		expect(phaseIndexIn(dbPhases, 'flash_backup')).toBe(dbPhases.findIndex((p) => p.key === 'uploading'));
		expect(phaseIndexIn(dbPhases, null)).toBe(0);
		expect(phaseIndexIn(dbPhases, 'mystery')).toBe(dbPhases.length - 1);
	});
});
