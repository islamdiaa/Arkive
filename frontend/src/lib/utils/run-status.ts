/**
 * Shared status-mapping helpers.
 *
 * The backend can emit run statuses: running, success, partial, failed,
 * cancelled, skipped. Anything that is not an unambiguous success must never
 * render as green.
 */

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

const TONES: Record<string, StatusTone> = {
	success: 'success',
	completed: 'success',
	ok: 'success',
	healthy: 'success',
	online: 'success',
	connected: 'success',
	passed: 'success',
	partial: 'warning',
	warning: 'warning',
	interrupted: 'warning',
	degraded: 'warning',
	failed: 'danger',
	error: 'danger',
	running: 'info',
	in_progress: 'info',
	pending: 'info',
	queued: 'info',
	cancelled: 'neutral',
	skipped: 'neutral',
	unknown: 'neutral',
};

export function statusTone(status: string | null | undefined): StatusTone {
	if (!status) return 'neutral';
	return TONES[String(status).toLowerCase()] ?? 'neutral';
}

// ── System health (dashboard "System" card / command strip) ──

export interface HealthDisplay {
	label: string;
	gradient: string;
}

export function healthDisplay(health: string | null | undefined): HealthDisplay {
	switch (health) {
		case 'healthy':
			return { label: 'Healthy', gradient: 'from-success to-info' };
		case 'degraded':
			return { label: 'Degraded', gradient: 'from-warning to-warning' };
		case 'error':
			return { label: 'Error', gradient: 'from-danger to-danger' };
		default:
			return { label: 'Unknown', gradient: 'from-border-muted to-border-muted' };
	}
}

/**
 * Health label for the dashboard. Prefers the backend's `health` alias, and
 * never shows green while the payload itself reports overdue backups (status
 * "error") or a failed last restore drill (at least "degraded").
 */
export function dashboardHealth(status: Record<string, any> | null | undefined): string {
	if (!status) return 'unknown';
	const raw = status.health ?? (status.status === 'ok' ? 'healthy' : status.status);
	let health = typeof raw === 'string' && raw ? raw : 'unknown';
	if (status.backup_freshness?.stale) return 'error';
	if (status.last_drill?.status === 'failed' && health === 'healthy') health = 'degraded';
	return health;
}

// ── Storage target health ──

const HEALTHY_TARGET_STATUSES = new Set(['ok', 'healthy', 'online', 'connected']);

export function isHealthyTargetStatus(status: string | null | undefined): boolean {
	return !!status && HEALTHY_TARGET_STATUSES.has(status.toLowerCase());
}

// ── Backup progress panel ──

export type PanelStatus = 'idle' | 'running' | 'success' | 'partial' | 'failed' | 'cancelled' | 'skipped';

/** Map the `status` field of a `backup:completed` event to a panel state. */
export function panelStatusForCompletion(status: unknown): PanelStatus {
	if (status === undefined || status === null || status === '') return 'success';
	switch (String(status).toLowerCase()) {
		case 'success':
			return 'success';
		case 'partial':
			return 'partial';
		case 'failed':
		case 'error':
			return 'failed';
		case 'cancelled':
			return 'cancelled';
		case 'skipped':
			return 'skipped';
		default:
			// Unknown terminal status: never claim success.
			return 'partial';
	}
}

/** Map a `backup:cancelled` event payload to a panel state. */
export function panelStatusForCancellation(data: { status?: unknown } | null | undefined): PanelStatus {
	return data?.status === 'skipped' ? 'skipped' : 'cancelled';
}

/**
 * Conflict events describe a *different* run that could not start because
 * another backup held the lock. They must not end the run currently shown.
 */
export function isConflictTerminalEvent(data: Record<string, unknown> | null | undefined): boolean {
	if (!data) return false;
	return data.status === 'skipped' || data.error_category === 'conflict';
}

/**
 * True when a terminal event belongs to another run while we are tracking an
 * active run (so it must not overwrite the active run's state).
 */
export function shouldIgnoreTerminalEvent(
	active: { runId: string | null; status: PanelStatus },
	eventRunId: string | null | undefined,
): boolean {
	return active.status === 'running' && !!active.runId && !!eventRunId && eventRunId !== active.runId;
}

// ── Trust score ──

/**
 * Resolve the dashboard trust score. The status API returns `trust_score: 0`
 * when no verification has ever run; that must render as "no score", not 0.
 */
export function resolveTrustScore(status: Record<string, any> | null | undefined): number | null {
	if (!status) return null;
	const neverVerified = !status.verification_status && !status.last_verified_at;
	if (neverVerified) return null;
	const score = status.trust_score ?? status.verification_status?.trust_score;
	return typeof score === 'number' ? score : null;
}
