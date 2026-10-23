import { describe, expect, it } from 'vitest';

import {
	FALLBACK_DEFAULT_EVENTS,
	FALLBACK_NOTIFICATION_EVENTS,
	eventLabel,
	fallbackEventCatalog,
	normalizeEventCatalog,
	partitionEvents,
	toggleEventValue,
} from './notification-events';

const LEGACY = ['backup.started', 'restore.completed', 'discovery.completed', 'system.warning'];

describe('fallback catalog', () => {
	it('matches the backend catalog values', () => {
		expect(FALLBACK_NOTIFICATION_EVENTS.map((e) => e.value)).toEqual([
			'backup.success',
			'backup.partial',
			'backup.failed',
			'backup.stale',
			'verification.failed',
			'drill.failed',
			'target.error',
			'system.startup',
			'system.shutdown',
		]);
	});

	it('does not offer legacy events the backend now rejects', () => {
		const values = FALLBACK_NOTIFICATION_EVENTS.map((e) => e.value);
		for (const legacy of LEGACY) expect(values).not.toContain(legacy);
	});

	it('only defaults to events in the catalog', () => {
		const values = new Set(FALLBACK_NOTIFICATION_EVENTS.map((e) => e.value));
		for (const value of FALLBACK_DEFAULT_EVENTS) expect(values.has(value)).toBe(true);
	});

	it('returns a fresh copy each time', () => {
		const a = fallbackEventCatalog();
		a.defaults.push('mutated');
		expect(fallbackEventCatalog().defaults).not.toContain('mutated');
	});
});

describe('normalizeEventCatalog', () => {
	it('keeps a well-formed server response', () => {
		const raw = {
			events: [
				{ value: 'backup.failed', label: 'Backup failed', description: 'A backup run failed' },
				{ value: 'new.event', label: 'New event', description: 'Added by a newer backend' },
			],
			defaults: ['backup.failed'],
		};
		expect(normalizeEventCatalog(raw)).toEqual(raw);
	});

	it('drops malformed and duplicate entries and fills missing labels', () => {
		const result = normalizeEventCatalog({
			events: [
				null,
				{ label: 'no value' },
				{ value: 'backup.failed' },
				{ value: 'backup.failed', label: 'Duplicate' },
				'string-entry',
			],
			defaults: ['backup.failed', 'unknown.default', 42],
		});
		expect(result.events).toEqual([{ value: 'backup.failed', label: 'backup.failed', description: '' }]);
		expect(result.defaults).toEqual(['backup.failed']);
	});

	it('falls back to the static catalog for unusable responses', () => {
		expect(normalizeEventCatalog(null)).toEqual(fallbackEventCatalog());
		expect(normalizeEventCatalog({})).toEqual(fallbackEventCatalog());
		expect(normalizeEventCatalog({ events: [] })).toEqual(fallbackEventCatalog());
	});

	it('uses the fallback defaults (filtered) when the response has none', () => {
		const result = normalizeEventCatalog({
			events: [{ value: 'backup.failed', label: 'Backup failed', description: '' }],
		});
		expect(result.defaults).toEqual(['backup.failed']);
	});
});

describe('partitionEvents', () => {
	const catalog = fallbackEventCatalog();

	it('splits stored events into kept and legacy values', () => {
		const result = partitionEvents(['backup.failed', 'backup.started', 'target.error', 'system.warning'], catalog);
		expect(result.kept).toEqual(['backup.failed', 'target.error']);
		expect(result.dropped).toEqual(['backup.started', 'system.warning']);
	});

	it('keeps the wildcard subscription', () => {
		expect(partitionEvents(['*'], catalog)).toEqual({ kept: ['*'], dropped: [] });
	});

	it('dedupes and ignores non-string values', () => {
		const result = partitionEvents(['backup.failed', 'backup.failed', 7, null, 'x', 'x'], catalog);
		expect(result).toEqual({ kept: ['backup.failed'], dropped: ['x'] });
	});

	it('handles channels with no event list', () => {
		expect(partitionEvents(undefined, catalog)).toEqual({ kept: [], dropped: [] });
		expect(partitionEvents('backup.failed', catalog)).toEqual({ kept: [], dropped: [] });
	});
});

describe('eventLabel / toggleEventValue', () => {
	const catalog = fallbackEventCatalog();

	it('labels known values and falls back to the raw value', () => {
		expect(eventLabel('backup.stale', catalog)).toBe('Backups overdue');
		expect(eventLabel('*', catalog)).toBe('All events');
		expect(eventLabel('system.warning', catalog)).toBe('system.warning');
	});

	it('toggles membership without mutating the input', () => {
		const selected = ['backup.failed'];
		expect(toggleEventValue(selected, 'target.error')).toEqual(['backup.failed', 'target.error']);
		expect(toggleEventValue(selected, 'backup.failed')).toEqual([]);
		expect(selected).toEqual(['backup.failed']);
	});
});
