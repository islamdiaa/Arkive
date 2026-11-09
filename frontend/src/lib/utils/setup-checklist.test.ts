import { describe, expect, it } from 'vitest';

import {
	checklistVisible,
	findStep,
	normalizeChecklist,
	progressLabel,
	progressPercent,
	restoreTestBlocker,
	SETUP_STEP_IDS,
	SETUP_STEP_LINKS,
	stepDescription,
	stepDone,
	stepTitle,
	type SetupChecklist,
} from './setup-checklist';

function payload(states: Record<string, string>, extra: Record<string, unknown> = {}) {
	return {
		complete: false,
		completed_count: 0,
		total: 5,
		dismissed: false,
		dismissed_at: null,
		steps: SETUP_STEP_IDS.map((id) => ({
			id,
			state: states[id] ?? 'todo',
			done: (states[id] ?? 'todo') === 'done',
			detail: null,
			completed_at: null,
			...(id === 'restore_test' ? { method: 'drill' } : {}),
		})),
		...extra,
	};
}

describe('normalizeChecklist', () => {
	it('keeps a well-formed payload and recounts progress from the steps', () => {
		const checklist = normalizeChecklist(payload({ first_backup: 'done', offsite_target: 'done' }))!;
		expect(checklist.steps.map((s) => s.id)).toEqual(SETUP_STEP_IDS);
		expect(checklist.completed_count).toBe(2);
		expect(checklist.total).toBe(5);
		expect(checklist.complete).toBe(false);
		expect(findStep(checklist, 'restore_test')?.method).toBe('drill');
	});

	it('marks the checklist complete only when every step is done', () => {
		const all = Object.fromEntries(SETUP_STEP_IDS.map((id) => [id, 'done']));
		expect(normalizeChecklist(payload(all))!.complete).toBe(true);
	});

	it('derives `done` from the state, not a stray flag', () => {
		const raw = payload({});
		raw.steps[0] = { ...raw.steps[0], state: 'attention', done: true };
		const checklist = normalizeChecklist(raw)!;
		expect(checklist.steps[0].done).toBe(false);
		expect(checklist.completed_count).toBe(0);
	});

	it('rejects payloads that are not a checklist', () => {
		expect(normalizeChecklist(null)).toBeNull();
		expect(normalizeChecklist({})).toBeNull();
		expect(normalizeChecklist({ detail: 'Not Found' })).toBeNull();
		expect(normalizeChecklist({ steps: [] })).toBeNull();
		expect(normalizeChecklist('nope')).toBeNull();
	});

	it('carries dismissal through', () => {
		const checklist = normalizeChecklist(payload({}, { dismissed: true, dismissed_at: '2026-11-09T10:00:00Z' }))!;
		expect(checklist.dismissed).toBe(true);
		expect(checklist.dismissed_at).toBe('2026-11-09T10:00:00Z');
	});
});

describe('visibility and progress', () => {
	it('is visible until dismissed', () => {
		const checklist = normalizeChecklist(payload({}))!;
		expect(checklistVisible(checklist)).toBe(true);
		expect(checklistVisible({ ...checklist, dismissed: true })).toBe(false);
		expect(checklistVisible(null)).toBe(false);
		expect(checklistVisible(undefined)).toBe(false);
	});

	it('formats progress', () => {
		expect(progressLabel({ completed_count: 3, total: 5 })).toBe('3 of 5 done');
		expect(progressPercent({ completed_count: 3, total: 5 })).toBe(60);
		expect(progressPercent({ completed_count: 0, total: 0 })).toBe(0);
		expect(progressPercent({ completed_count: 9, total: 5 })).toBe(100);
	});

	it('reports whether a step is done', () => {
		const checklist = normalizeChecklist(payload({ recovery_kit: 'done' }))!;
		expect(stepDone(checklist, 'recovery_kit')).toBe(true);
		expect(stepDone(checklist, 'notifications')).toBe(false);
		expect(stepDone(null, 'recovery_kit')).toBe(false);
	});
});

describe('restore test', () => {
	it('is blocked until the first backup is done', () => {
		const before: SetupChecklist = normalizeChecklist(payload({ first_backup: 'running' }))!;
		expect(restoreTestBlocker(before)).toBe('Available once the first backup has completed.');
		const after: SetupChecklist = normalizeChecklist(payload({ first_backup: 'done' }))!;
		expect(restoreTestBlocker(after)).toBeNull();
	});

	it('describes the drill and the file-restore fallback differently', () => {
		expect(stepDescription({ id: 'restore_test', method: 'drill' })).toMatch(/restore drill/);
		expect(stepDescription({ id: 'restore_test', method: 'verification' })).toMatch(/No databases here/);
	});
});

describe('copy and links', () => {
	it('has a title and description for every step', () => {
		for (const id of SETUP_STEP_IDS) {
			expect(stepTitle(id)).not.toBe(id);
			expect(stepDescription({ id })).not.toBe('');
		}
		expect(stepTitle('something_new')).toBe('something_new');
	});

	it('links page-based steps to their settings pages', () => {
		expect(SETUP_STEP_LINKS.offsite_target?.href).toBe('/settings/targets');
		expect(SETUP_STEP_LINKS.notifications?.href).toBe('/settings/notifications');
		expect(SETUP_STEP_LINKS.recovery_kit?.href).toBe('/settings/recovery-kit');
		// The first backup and the restore test are buttons, not links.
		expect(SETUP_STEP_LINKS.first_backup).toBeUndefined();
		expect(SETUP_STEP_LINKS.restore_test).toBeUndefined();
	});
});
