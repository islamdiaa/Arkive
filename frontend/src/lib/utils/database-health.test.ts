import { describe, expect, it } from 'vitest';
import type { DatabaseSizePoint } from '$lib/types';
import {
	drillSummary,
	formatSizeChange,
	healthStatusStyle,
	indexHealth,
	needsAttention,
	trendGeometry,
} from './database-health';

const point = (size: number | null, status = 'success', shrunk = false): DatabaseSizePoint => ({
	run_id: 'r',
	at: '2026-11-01T02:00:00Z',
	status,
	size_bytes: size,
	shrunk,
});

describe('healthStatusStyle', () => {
	it('labels every backend status', () => {
		expect(healthStatusStyle('ok')).toEqual({ label: 'Healthy', badge: 'bg-success-bg text-success' });
		expect(healthStatusStyle('warning').label).toBe('Needs attention');
		expect(healthStatusStyle('failed').badge).toContain('text-danger');
		expect(healthStatusStyle('never_dumped').label).toBe('Not backed up yet');
		expect(healthStatusStyle('excluded').label).toBe('Excluded');
	});

	it('falls back for unknown statuses', () => {
		expect(healthStatusStyle('mystery')).toEqual({ label: 'mystery', badge: 'bg-neutral-bg text-neutral' });
		expect(healthStatusStyle(undefined).label).toBe('Unknown');
	});
});

describe('needsAttention', () => {
	it('is true for warnings, failures and databases never backed up', () => {
		expect(['ok', 'warning', 'failed', 'never_dumped', 'excluded'].map((status) => needsAttention({ status }))).toEqual([
			false,
			true,
			true,
			true,
			false,
		]);
	});
});

describe('indexHealth', () => {
	it('indexes by key and skips entries without one', () => {
		const map = indexHealth([{ key: 'a' } as any, { key: '' } as any, null as any]);
		expect([...map.keys()]).toEqual(['a']);
		expect(indexHealth(undefined).size).toBe(0);
	});
});

describe('formatSizeChange', () => {
	it('formats growth, shrink and no change', () => {
		expect(formatSizeChange(1.234)).toBe('+1.2%');
		expect(formatSizeChange(-68.3)).toBe('−68%');
		expect(formatSizeChange(0.01)).toBe('±0%');
		expect(formatSizeChange(null)).toBe('');
		expect(formatSizeChange(Number.NaN)).toBe('');
	});
});

describe('drillSummary', () => {
	it('describes each drill outcome', () => {
		expect(drillSummary(null)).toMatchObject({ label: 'Never drilled', tone: 'muted' });
		expect(drillSummary({ status: 'passed', detail: '42 tables' } as any)).toMatchObject({
			label: 'Passed',
			tone: 'success',
			title: '42 tables',
		});
		expect(drillSummary({ status: 'failed', detail: null } as any)).toMatchObject({ label: 'Failed', tone: 'danger' });
		expect(drillSummary({ status: 'skipped', detail: 'over the size cap' } as any)).toMatchObject({
			label: 'Not tested',
			title: 'over the size cap',
		});
	});
});

describe('trendGeometry', () => {
	it('scales sizes between the box edges, oldest on the left', () => {
		const { line, dots } = trendGeometry([point(100), point(200), point(150)], 106, 26, 3);
		expect(dots.map((d) => d.x)).toEqual([3, 53, 103]);
		expect(dots.map((d) => d.y)).toEqual([23, 3, 13]);
		expect(line).toBe('3,23 53,3 103,13');
	});

	it('puts failed dumps on the bottom edge and leaves them out of the line', () => {
		const { line, dots } = trendGeometry([point(100), point(null, 'failed'), point(100)], 106, 26, 3);
		expect(dots[1]).toMatchObject({ status: 'failed', y: 23 });
		expect(line).toBe('3,3 103,3'); // equal sizes: a flat line along the top
	});

	it('keeps a few percent of churn visually flat', () => {
		// 100 -> 102 MB is 2%; the box spans at least 20% of the largest dump.
		const { dots } = trendGeometry([point(100), point(102)], 106, 26, 3);
		expect(dots.map((d) => d.y)).toEqual([5, 3]);
		// A real drop still reaches the bottom.
		const drop = trendGeometry([point(100), point(30)], 106, 26, 3);
		expect(drop.dots.map((d) => d.y)).toEqual([3, 23]);
	});

	it('handles empty, single-point and all-zero trends', () => {
		expect(trendGeometry([], 100, 20)).toEqual({ line: '', dots: [] });
		expect(trendGeometry(undefined, 100, 20)).toEqual({ line: '', dots: [] });
		const single = trendGeometry([point(5, 'success', true)], 100, 20, 0);
		expect(single.line).toBe('');
		expect(single.dots).toEqual([{ x: 50, y: 0, status: 'success', shrunk: true, size: 5, at: '2026-11-01T02:00:00Z' }]);
		expect(trendGeometry([point(0), point(0)], 100, 20, 0).dots.map((d) => d.y)).toEqual([10, 10]);
	});
});
