/**
 * "Finish protecting this server" checklist (GET /api/setup-checklist).
 *
 * The backend judges each step from evidence (a good backup run, a passed
 * restore drill, an enabled offsite target, ...); this module only holds the
 * copy and the small decisions the dashboard card makes.
 */

export type SetupStepId = 'first_backup' | 'restore_test' | 'offsite_target' | 'notifications' | 'recovery_kit';

/** `attention` means the last attempt went wrong (failed run, failed drill, outdated kit). */
export type SetupStepState = 'done' | 'todo' | 'running' | 'attention';

export interface SetupChecklistStep {
	id: SetupStepId | string;
	state: SetupStepState | string;
	done: boolean;
	detail: string | null;
	completed_at: string | null;
	/** restore_test only: a restore drill when the server has databases, else a verification file restore. */
	method?: 'drill' | 'verification' | string;
}

export interface SetupChecklist {
	complete: boolean;
	completed_count: number;
	total: number;
	dismissed: boolean;
	dismissed_at: string | null;
	steps: SetupChecklistStep[];
}

export const SETUP_STEP_IDS: SetupStepId[] = [
	'first_backup',
	'restore_test',
	'offsite_target',
	'notifications',
	'recovery_kit',
];

const STEP_TITLES: Record<SetupStepId, string> = {
	first_backup: 'Run the first backup',
	restore_test: 'Prove a restore works',
	offsite_target: 'Keep a copy off this server',
	notifications: 'Get told when backups fail',
	recovery_kit: 'Save the recovery kit',
};

const STEP_DESCRIPTIONS: Record<SetupStepId, string> = {
	first_backup:
		'Until a backup completes cleanly there is nothing to rely on: one that finishes with warnings may be missing a database or a folder.',
	restore_test: 'A restore drill loads recent database dumps into a throwaway container to prove they restore.',
	offsite_target: 'A cloud or remote target survives what a local disk does not: a failed array, a fire, theft.',
	notifications: 'Add a notification channel so failed or overdue backups reach you instead of going unnoticed.',
	recovery_kit: "Without the encryption password stored somewhere else, the backups can't be opened if this server dies.",
};

// Used when no database here can be drilled: none at all, only Redis/MongoDB
// (drills don't test those), or every dump is over the drill size limit.
const VERIFICATION_RESTORE_DESCRIPTION =
	'No database here can be restore-drilled, so a restore test pulls a file back out of the latest snapshot and checks its checksum.';

/** Steps whose action is a settings page rather than a button. */
export const SETUP_STEP_LINKS: Partial<Record<SetupStepId, { href: string; label: string }>> = {
	offsite_target: { href: '/settings/targets', label: 'Add offsite target' },
	notifications: { href: '/settings/notifications', label: 'Add notification channel' },
	recovery_kit: { href: '/settings/recovery-kit', label: 'Save recovery kit' },
};

export function stepTitle(id: string): string {
	return STEP_TITLES[id as SetupStepId] ?? id;
}

export function stepDescription(step: Pick<SetupChecklistStep, 'id' | 'method'>): string {
	if (step.id === 'restore_test' && step.method === 'verification') return VERIFICATION_RESTORE_DESCRIPTION;
	return STEP_DESCRIPTIONS[step.id as SetupStepId] ?? '';
}

function isObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null;
}

function str(value: unknown): string | null {
	return typeof value === 'string' && value ? value : null;
}

/**
 * Validate an API payload. Returns null for anything that isn't a checklist
 * (an older backend, an error body), so the dashboard simply hides the card.
 */
export function normalizeChecklist(raw: unknown): SetupChecklist | null {
	if (!isObject(raw) || !Array.isArray(raw.steps)) return null;
	const steps: SetupChecklistStep[] = raw.steps.filter(isObject).map((s) => {
		const state = str(s.state) ?? (s.done === true ? 'done' : 'todo');
		const step: SetupChecklistStep = {
			id: String(s.id ?? ''),
			state,
			done: state === 'done',
			detail: str(s.detail),
			completed_at: str(s.completed_at),
		};
		if (typeof s.method === 'string') step.method = s.method;
		return step;
	});
	if (steps.length === 0) return null;
	const completed = steps.filter((s) => s.done).length;
	return {
		complete: completed === steps.length,
		completed_count: completed,
		total: steps.length,
		dismissed: raw.dismissed === true,
		dismissed_at: str(raw.dismissed_at),
		steps,
	};
}

/** The card shows until the user dismisses a completed checklist. */
export function checklistVisible(checklist: SetupChecklist | null | undefined): boolean {
	return !!checklist && !checklist.dismissed && checklist.steps.length > 0;
}

export function findStep(
	checklist: SetupChecklist | null | undefined,
	id: SetupStepId,
): SetupChecklistStep | undefined {
	return checklist?.steps.find((s) => s.id === id);
}

export function stepDone(checklist: SetupChecklist | null | undefined, id: SetupStepId): boolean {
	return findStep(checklist, id)?.done === true;
}

/** "3 of 5 done" */
export function progressLabel(checklist: Pick<SetupChecklist, 'completed_count' | 'total'>): string {
	return `${checklist.completed_count} of ${checklist.total} done`;
}

/** Width of the progress bar, 0-100. */
export function progressPercent(checklist: Pick<SetupChecklist, 'completed_count' | 'total'>): number {
	if (!checklist.total) return 0;
	return Math.round((Math.min(checklist.completed_count, checklist.total) / checklist.total) * 100);
}

/**
 * Why the restore test can't run yet, or null when it can. Drills and
 * verification both restore from a snapshot, so a backup must exist first.
 */
export function restoreTestBlocker(checklist: SetupChecklist | null | undefined): string | null {
	return stepDone(checklist, 'first_backup') ? null : 'Available once the first backup has completed.';
}
