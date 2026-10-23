/**
 * Restore drill display helpers.
 */
import type { LastDrill } from '$lib/types';

type DrillCounts = Pick<LastDrill, 'databases_tested' | 'databases_passed' | 'databases_failed'>;

function count(n: unknown): number {
	return typeof n === 'number' && Number.isFinite(n) ? n : 0;
}

/** "3 tested · 3 passed · 0 failed" */
export function drillCountsLabel(drill: Partial<DrillCounts> | null | undefined): string {
	return `${count(drill?.databases_tested)} tested · ${count(drill?.databases_passed)} passed · ${count(drill?.databases_failed)} failed`;
}

/** Timestamp that best describes when a drill happened. */
export function drillTimestamp(drill: Pick<LastDrill, 'started_at' | 'completed_at'> | null | undefined): string | null {
	return drill?.completed_at || drill?.started_at || null;
}

/** True while any drill in the list is still running. */
export function hasRunningDrill(drills: Array<Pick<LastDrill, 'status'>> | null | undefined): boolean {
	return Array.isArray(drills) && drills.some((d) => d?.status === 'running');
}

/**
 * Message for a failed POST /api/drills/run. 409 means a drill is already
 * running (not an error for the user); 503 means drills cannot run right now.
 */
export function runDrillErrorMessage(status: number | undefined, message: string | undefined): {
	tone: 'info' | 'error';
	message: string;
} {
	if (status === 409) {
		return { tone: 'info', message: 'A restore drill is already running.' };
	}
	if (status === 503) {
		// The backend's own 503 text already reads "Restore drills are unavailable: ...".
		if (message && /restore drill/i.test(message)) return { tone: 'error', message };
		return {
			tone: 'error',
			message: `Restore drills are unavailable right now${message ? `: ${message}` : '.'}`,
		};
	}
	return { tone: 'error', message: message || 'Failed to start restore drill' };
}

/** Toast for a drill:completed SSE event. */
export function drillCompletedToast(data: Record<string, unknown>): {
	type: 'success' | 'error' | 'warning' | 'info';
	message: string;
} {
	const status = String(data.status ?? '');
	const tested = count(data.databases_tested);
	const passed = count(data.databases_passed);
	const failed = count(data.databases_failed);
	switch (status) {
		case 'passed':
			return { type: 'success', message: `Restore drill passed (${passed}/${tested} databases restored)` };
		case 'failed':
			return {
				type: 'error',
				message: tested > 0
					? `Restore drill failed: ${failed} of ${tested} databases did not restore`
					: 'Restore drill failed',
			};
		case 'skipped':
			return { type: 'warning', message: 'Restore drill skipped — no database dumps could be tested' };
		default:
			return { type: 'info', message: `Restore drill finished (${status || 'unknown'})` };
	}
}
