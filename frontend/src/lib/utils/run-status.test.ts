import { describe, expect, it } from 'vitest';

import {
	healthDisplay,
	isConflictTerminalEvent,
	isHealthyTargetStatus,
	panelStatusForCancellation,
	panelStatusForCompletion,
	resolveTrustScore,
	shouldIgnoreTerminalEvent,
	statusTone,
} from './run-status';

describe('statusTone', () => {
	it('only maps unambiguous success to the success tone', () => {
		expect(statusTone('success')).toBe('success');
		expect(statusTone('partial')).toBe('warning');
		expect(statusTone('failed')).toBe('danger');
		expect(statusTone('running')).toBe('info');
		expect(statusTone('cancelled')).toBe('neutral');
		expect(statusTone('skipped')).toBe('neutral');
		expect(statusTone(undefined)).toBe('neutral');
		expect(statusTone('something-new')).toBe('neutral');
	});
});

describe('healthDisplay', () => {
	it('maps health to label and gradient, with Unknown when missing', () => {
		expect(healthDisplay('healthy')).toEqual({ label: 'Healthy', gradient: 'from-success to-info' });
		expect(healthDisplay('degraded').label).toBe('Degraded');
		expect(healthDisplay('degraded').gradient).toContain('warning');
		expect(healthDisplay('error').gradient).toContain('danger');
		expect(healthDisplay(undefined).label).toBe('Unknown');
		expect(healthDisplay(null).gradient).not.toContain('success');
	});
});

describe('panelStatusForCompletion', () => {
	it('maps backup:completed statuses without ever upgrading to success', () => {
		expect(panelStatusForCompletion('success')).toBe('success');
		expect(panelStatusForCompletion('partial')).toBe('partial');
		expect(panelStatusForCompletion('failed')).toBe('failed');
		expect(panelStatusForCompletion('weird')).toBe('partial');
	});

	it('treats a missing status as success for older backends', () => {
		expect(panelStatusForCompletion(undefined)).toBe('success');
	});
});

describe('cancellation and conflict events', () => {
	it('distinguishes skipped from cancelled', () => {
		expect(panelStatusForCancellation({ status: 'skipped' })).toBe('skipped');
		expect(panelStatusForCancellation({})).toBe('cancelled');
		expect(panelStatusForCancellation(undefined)).toBe('cancelled');
	});

	it('recognises conflict payloads', () => {
		expect(isConflictTerminalEvent({ status: 'skipped' })).toBe(true);
		expect(isConflictTerminalEvent({ error_category: 'conflict' })).toBe(true);
		expect(isConflictTerminalEvent({ status: 'failed', error_category: 'disk' })).toBe(false);
	});

	it('ignores terminal events for other runs only while a run is active', () => {
		expect(shouldIgnoreTerminalEvent({ runId: 'a', status: 'running' }, 'b')).toBe(true);
		expect(shouldIgnoreTerminalEvent({ runId: 'a', status: 'running' }, 'a')).toBe(false);
		expect(shouldIgnoreTerminalEvent({ runId: 'a', status: 'success' }, 'b')).toBe(false);
		expect(shouldIgnoreTerminalEvent({ runId: null, status: 'running' }, 'b')).toBe(false);
	});
});

describe('isHealthyTargetStatus', () => {
	it('accepts the statuses the backend writes for healthy targets', () => {
		expect(isHealthyTargetStatus('ok')).toBe(true);
		expect(isHealthyTargetStatus('healthy')).toBe(true);
		expect(isHealthyTargetStatus('error')).toBe(false);
		expect(isHealthyTargetStatus('unknown')).toBe(false);
	});
});

describe('resolveTrustScore', () => {
	it('returns null when no verification has ever run', () => {
		expect(resolveTrustScore({ trust_score: 0, last_verified_at: null, verification_status: null })).toBeNull();
		expect(resolveTrustScore(null)).toBeNull();
	});

	it('returns the score once verification has run', () => {
		expect(
			resolveTrustScore({
				trust_score: 0,
				last_verified_at: '2026-10-01T00:00:00Z',
				verification_status: { trust_score: 0 },
			}),
		).toBe(0);
		expect(resolveTrustScore({ trust_score: 94, last_verified_at: '2026-10-01T00:00:00Z' })).toBe(94);
	});
});
