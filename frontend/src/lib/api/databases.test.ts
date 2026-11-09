/**
 * Database health API: demo-mode data must have the shape the backend
 * returns (GET /api/databases/health), and the database list must carry the
 * key the Databases page joins on.
 */
import { describe, expect, it } from 'vitest';
import { mockApi } from './mock';

// Keep in sync with backend/tests/unit/test_database_health.py::HEALTH_ITEM_FIELDS.
const HEALTH_ITEM_FIELDS = [
	'key',
	'container_name',
	'db_type',
	'db_name',
	'host_path',
	'excluded',
	'status',
	'issues',
	'notes',
	'last_attempt',
	'last_success_at',
	'last_success_run_id',
	'size_bytes',
	'integrity_check',
	'baseline_bytes',
	'size_change_percent',
	'shrunk',
	'shrink_percent',
	'trend',
	'last_drill',
	'restorable_as_of',
].sort();

describe('database health mock', () => {
	it('matches the backend response shape', async () => {
		const health = await mockApi.getDatabaseHealth();
		expect(Object.keys(health).sort()).toEqual(
			['items', 'databases', 'total', 'summary', 'policy', 'latest_upload_at', 'generated_at'].sort(),
		);
		expect(health.total).toBe(health.items.length);
		for (const item of health.items) {
			expect(Object.keys(item).sort()).toEqual(HEALTH_ITEM_FIELDS);
			for (const p of item.trend) {
				expect(Object.keys(p).sort()).toEqual(['at', 'run_id', 'shrunk', 'size_bytes', 'status']);
			}
		}
		expect(Object.keys(health.policy).sort()).toEqual(
			['baseline_dumps', 'min_baseline_dumps', 'shrink_alert_percent', 'shrink_min_mb'].sort(),
		);
	});

	it('has a health entry for every listed database', async () => {
		const list = await mockApi.listDatabases();
		const health = await mockApi.getDatabaseHealth();
		const keys = new Set(health.items.map((i: any) => i.key));
		for (const db of list.databases) {
			expect(keys.has(db.key)).toBe(true);
		}
	});

	it('never flags a Redis dump as shrunk, like the backend', async () => {
		const health = await mockApi.getDatabaseHealth();
		const redis = health.items.filter((i: any) => i.db_type === 'redis');
		expect(redis.length).toBeGreaterThan(0);
		for (const item of redis) {
			expect(item.shrunk).toBe(false);
			expect(item.baseline_bytes).toBeNull();
			expect(item.notes.some((n: string) => n.includes('not checked for shrinking'))).toBe(true);
		}
	});

	it('accepting a shrunk size clears the warning', async () => {
		const health = await mockApi.getDatabaseHealth();
		const shrunk = health.items.find((i: any) => i.shrunk);
		expect(shrunk).toBeTruthy();

		const result = await mockApi.acceptDatabaseSize(shrunk.key);

		expect(result.accepted_from).toBe(shrunk.last_success_at);
		const after = (await mockApi.getDatabaseHealth()).items.find((i: any) => i.key === shrunk.key);
		expect(after.shrunk).toBe(false);
		expect(after.status).toBe('ok');
	});
});
