/**
 * Demo mode must carry /api/status `offsite` with every field the backend
 * returns (services/offsite.py evaluate_offsite), so the dashboard banner
 * renders the same way against the mock as against a real server.
 */
import { describe, expect, it } from 'vitest';
import { mockApi } from './mock';

describe('mock /status offsite', () => {
	it('matches the backend offsite shape', async () => {
		const status = await mockApi.getStatus();
		const offsite = status.offsite;
		expect(Object.keys(offsite).sort()).toEqual(
			[
				'enabled_targets',
				'has_offsite_copy',
				'local_targets',
				'message',
				'offsite_targets',
				'uncertain',
				'unused_offsite_targets',
				'warning',
			].sort()
		);
		expect(typeof offsite.has_offsite_copy).toBe('boolean');
		expect(typeof offsite.warning).toBe('boolean');
		expect(typeof offsite.uncertain).toBe('boolean');
		expect(Array.isArray(offsite.local_targets)).toBe(true);
		expect(Array.isArray(offsite.unused_offsite_targets)).toBe(true);
	});
});
