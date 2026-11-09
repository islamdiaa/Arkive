import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';
import DumpSizeTrend from './DumpSizeTrend.svelte';

const MIB = 1024 * 1024;
const points = [
	{ run_id: 'r1', at: '2026-11-01T02:00:00Z', status: 'success', size_bytes: 200 * MIB, shrunk: false },
	{ run_id: 'r2', at: '2026-11-02T02:00:00Z', status: 'failed', size_bytes: null, shrunk: false },
	{ run_id: 'r3', at: '2026-11-03T02:00:00Z', status: 'success', size_bytes: 40 * MIB, shrunk: true },
];

describe('DumpSizeTrend', () => {
	it('draws the size line and marks failed and shrunk dumps', () => {
		const { container } = render(DumpSizeTrend, { props: { points } });
		const svg = container.querySelector('[data-testid="dump-size-trend"]');
		expect(svg).toBeTruthy();
		expect(svg?.getAttribute('aria-label')).toBe('Dump size over the last 3 runs: 200 MB to 40 MB, 1 failed');
		expect(container.querySelector('polyline')).toBeTruthy();
		expect(container.querySelector('[data-status="failed"]')).toBeTruthy();
		expect(container.querySelector('[data-status="shrunk"]')).toBeTruthy();
	});

	it('shows a text fallback without history', () => {
		const { container } = render(DumpSizeTrend, { props: { points: [] } });
		expect(container.querySelector('svg')).toBeNull();
		expect(container.textContent).toContain('No dump history yet');
	});
});
