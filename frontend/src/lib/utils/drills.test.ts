import { describe, expect, it } from 'vitest';

import {
	drillCompletedToast,
	drillCountsLabel,
	drillTimestamp,
	hasRunningDrill,
	runDrillErrorMessage,
} from './drills';

describe('drillCountsLabel', () => {
	it('summarizes tested/passed/failed counts', () => {
		expect(drillCountsLabel({ databases_tested: 3, databases_passed: 2, databases_failed: 1 })).toBe(
			'3 tested · 2 passed · 1 failed',
		);
	});

	it('treats missing counts as zero', () => {
		expect(drillCountsLabel(null)).toBe('0 tested · 0 passed · 0 failed');
		expect(drillCountsLabel({ databases_tested: 2 })).toBe('2 tested · 0 passed · 0 failed');
	});
});

describe('drillTimestamp', () => {
	it('prefers completion time, then start time', () => {
		expect(drillTimestamp({ started_at: 'a', completed_at: 'b' })).toBe('b');
		expect(drillTimestamp({ started_at: 'a', completed_at: null })).toBe('a');
		expect(drillTimestamp({ started_at: null, completed_at: null })).toBeNull();
		expect(drillTimestamp(null)).toBeNull();
	});
});

describe('hasRunningDrill', () => {
	it('detects a running drill', () => {
		expect(hasRunningDrill([{ status: 'passed' }, { status: 'running' }])).toBe(true);
		expect(hasRunningDrill([{ status: 'passed' }, { status: 'failed' }])).toBe(false);
		expect(hasRunningDrill([])).toBe(false);
		expect(hasRunningDrill(null)).toBe(false);
	});
});

describe('runDrillErrorMessage', () => {
	it('treats 409 as an informational "already running"', () => {
		expect(runDrillErrorMessage(409, 'conflict')).toEqual({
			tone: 'info',
			message: 'A restore drill is already running.',
		});
	});

	it('explains 503 with the server reason', () => {
		expect(runDrillErrorMessage(503, 'Docker is not reachable')).toEqual({
			tone: 'error',
			message: 'Restore drills are unavailable right now: Docker is not reachable',
		});
		expect(runDrillErrorMessage(503, undefined).message).toBe('Restore drills are unavailable right now.');
		const backendText = 'Restore drills are unavailable: Docker or the backup engine is not configured';
		expect(runDrillErrorMessage(503, backendText)).toEqual({ tone: 'error', message: backendText });
	});

	it('passes other errors through', () => {
		expect(runDrillErrorMessage(500, 'boom')).toEqual({ tone: 'error', message: 'boom' });
		expect(runDrillErrorMessage(undefined, undefined)).toEqual({
			tone: 'error',
			message: 'Failed to start restore drill',
		});
	});
});

describe('drillCompletedToast', () => {
	it('reports a passing drill as success', () => {
		expect(drillCompletedToast({ status: 'passed', databases_tested: 3, databases_passed: 3, databases_failed: 0 })).toEqual({
			type: 'success',
			message: 'Restore drill passed (3/3 databases restored)',
		});
	});

	it('reports failures as errors with counts', () => {
		expect(drillCompletedToast({ status: 'failed', databases_tested: 3, databases_passed: 2, databases_failed: 1 })).toEqual({
			type: 'error',
			message: 'Restore drill failed: 1 of 3 databases did not restore',
		});
		expect(drillCompletedToast({ status: 'failed', databases_tested: 0 }).message).toBe('Restore drill failed');
	});

	it('warns on skipped drills and never claims success for unknown statuses', () => {
		expect(drillCompletedToast({ status: 'skipped' }).type).toBe('warning');
		expect(drillCompletedToast({ status: 'weird' }).type).toBe('info');
		expect(drillCompletedToast({}).type).toBe('info');
	});
});
