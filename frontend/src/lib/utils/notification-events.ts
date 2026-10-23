/**
 * Notification event catalog helpers.
 *
 * The canonical list is served by GET /api/notifications/events (backed by
 * backend/app/services/notification_events.py). The backend rejects unknown
 * event types with 422, so the UI must only ever send values from the catalog.
 * The static copy below is a fallback for when that request fails; keep it in
 * sync with the backend file.
 */
import type { NotificationEventCatalog, NotificationEventOption } from '$lib/types';

export const FALLBACK_NOTIFICATION_EVENTS: NotificationEventOption[] = [
	{ value: 'backup.success', label: 'Backup succeeded', description: 'A backup completed with every phase successful' },
	{ value: 'backup.partial', label: 'Backup incomplete', description: 'A snapshot was written but something was missed (a database, a path, a target)' },
	{ value: 'backup.failed', label: 'Backup failed', description: 'A backup run failed' },
	{ value: 'backup.stale', label: 'Backups overdue', description: 'A job has not produced a good backup within its schedule window' },
	{ value: 'verification.failed', label: 'Verification failed', description: 'A repository integrity check or restore test failed' },
	{ value: 'drill.failed', label: 'Restore drill failed', description: 'A database dump could not be restored into a test instance' },
	{ value: 'target.error', label: 'Storage target unreachable', description: 'A storage target stopped responding to health checks' },
	{ value: 'system.startup', label: 'Arkive started', description: 'The Arkive container started' },
	{ value: 'system.shutdown', label: 'Arkive stopped', description: 'The Arkive container is shutting down' },
];

export const FALLBACK_DEFAULT_EVENTS: string[] = [
	'backup.failed',
	'backup.partial',
	'backup.stale',
	'verification.failed',
	'drill.failed',
	'target.error',
];

/** The backend also accepts "*" (all events) on stored channels. */
export const WILDCARD_EVENT = '*';

export function fallbackEventCatalog(): NotificationEventCatalog {
	return {
		events: FALLBACK_NOTIFICATION_EVENTS.map((e) => ({ ...e })),
		defaults: [...FALLBACK_DEFAULT_EVENTS],
	};
}

/**
 * Validate a /notifications/events response. Malformed entries are dropped;
 * an unusable response falls back to the static catalog. Defaults are limited
 * to values present in the catalog.
 */
export function normalizeEventCatalog(raw: unknown): NotificationEventCatalog {
	const data = raw as { events?: unknown; defaults?: unknown } | null | undefined;
	const seen = new Set<string>();
	const events: NotificationEventOption[] = [];
	if (data && Array.isArray(data.events)) {
		for (const item of data.events) {
			if (!item || typeof item !== 'object') continue;
			const { value, label, description } = item as Record<string, unknown>;
			if (typeof value !== 'string' || !value || seen.has(value)) continue;
			seen.add(value);
			events.push({
				value,
				label: typeof label === 'string' && label ? label : value,
				description: typeof description === 'string' ? description : '',
			});
		}
	}
	if (events.length === 0) return fallbackEventCatalog();

	const rawDefaults = data && Array.isArray(data.defaults) ? data.defaults : FALLBACK_DEFAULT_EVENTS;
	const defaults = [...new Set(rawDefaults.filter((v): v is string => typeof v === 'string' && seen.has(v)))];
	return { events, defaults };
}

export interface EventPartition {
	/** Stored values the backend still accepts, in their original order. */
	kept: string[];
	/** Legacy values no longer in the catalog; dropped on save. */
	dropped: string[];
}

/** Split a channel's stored events into values to keep and legacy ones to drop. */
export function partitionEvents(stored: unknown, catalog: NotificationEventCatalog): EventPartition {
	const known = new Set(catalog.events.map((e) => e.value));
	const kept: string[] = [];
	const dropped: string[] = [];
	if (!Array.isArray(stored)) return { kept, dropped };
	for (const value of stored) {
		if (typeof value !== 'string') continue;
		const bucket = known.has(value) || value === WILDCARD_EVENT ? kept : dropped;
		if (!bucket.includes(value)) bucket.push(value);
	}
	return { kept, dropped };
}

/** Human label for an event value (falls back to the raw value). */
export function eventLabel(value: string, catalog: NotificationEventCatalog): string {
	if (value === WILDCARD_EVENT) return 'All events';
	return catalog.events.find((e) => e.value === value)?.label ?? value;
}

/** Toggle membership of an event value in a selection list (returns a new array). */
export function toggleEventValue(selected: string[], value: string): string[] {
	return selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value];
}
