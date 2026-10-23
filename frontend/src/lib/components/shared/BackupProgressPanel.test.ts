import { render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import BackupProgressPanel from './BackupProgressPanel.svelte';
import { backupRunning } from '$lib/stores/app';

type Handler = (event: { type: string; data: Record<string, unknown>; timestamp: string }) => void;

const mocks = vi.hoisted(() => ({
	listRuns: vi.fn(),
	getRun: vi.fn(),
	handlers: new Map<string, (event: any) => void>(),
	subscribe: vi.fn(),
}));

vi.mock('$lib/api/backup', () => ({
	backupApi: {
		listRuns: mocks.listRuns,
		getRun: mocks.getRun,
	},
}));

vi.mock('$lib/stores/sse', () => ({
	subscribe: mocks.subscribe,
}));

function emit(type: string, data: Record<string, unknown>) {
	const handler = mocks.handlers.get(type) as Handler | undefined;
	if (!handler) throw new Error(`no handler registered for ${type}`);
	handler({ type, data, timestamp: new Date().toISOString() });
}

async function renderIdlePanel() {
	mocks.listRuns.mockResolvedValue({ items: [], total: 0 });
	const result = render(BackupProgressPanel);
	await waitFor(() => expect(mocks.listRuns).toHaveBeenCalled());
	await tick();
	return result;
}

function panel(container: HTMLElement) {
	return container.querySelector('[data-testid="backup-progress-panel"]');
}

describe('BackupProgressPanel', () => {
	beforeEach(() => {
		mocks.listRuns.mockReset();
		mocks.getRun.mockReset();
		mocks.getRun.mockResolvedValue({});
		mocks.handlers.clear();
		mocks.subscribe.mockReset();
		mocks.subscribe.mockImplementation((type: string, handler: (event: any) => void) => {
			mocks.handlers.set(type, handler);
			return () => mocks.handlers.delete(type);
		});
		sessionStorage.clear();
		backupRunning.set(false);
	});

	it('rehydrates visible running progress after refresh when a run is still active', async () => {
		sessionStorage.setItem(
			'arkive_backup_progress',
			JSON.stringify({
				runId: 'run-1',
				visible: true,
				currentPhaseIndex: 2,
				percent: 50,
				status: 'running',
				errorMessage: '',
				updatedAt: new Date().toISOString(),
			}),
		);
		mocks.listRuns.mockResolvedValue({
			items: [{ id: 'run-1', status: 'running' }],
			total: 1,
		});

		const { container } = render(BackupProgressPanel);

		await waitFor(() => {
			expect(screen.getByText('Backup Progress')).toBeInTheDocument();
		});

		expect(get(backupRunning)).toBe(true);
		expect(container.querySelector('[role="progressbar"]')).toHaveAttribute('aria-valuenow', '50');
	});

	it('clears stale running state when no active run exists after refresh', async () => {
		sessionStorage.setItem(
			'arkive_backup_progress',
			JSON.stringify({
				runId: 'run-1',
				visible: true,
				currentPhaseIndex: 1,
				percent: 25,
				status: 'running',
				errorMessage: '',
				updatedAt: new Date().toISOString(),
			}),
		);
		mocks.listRuns.mockResolvedValue({
			items: [],
			total: 0,
		});

		const { container } = render(BackupProgressPanel);

		await waitFor(() => {
			expect(get(backupRunning)).toBe(false);
		});

		expect(sessionStorage.getItem('arkive_backup_progress')).toBeNull();
		expect(container.querySelector('[role="progressbar"]')).toBeNull();
	});

	it('shows success only when backup:completed reports status success', async () => {
		const { container } = await renderIdlePanel();
		emit('backup:started', { run_id: 'r1' });
		emit('backup:completed', { run_id: 'r1', status: 'success', total_bytes: 10 });
		await tick();

		expect(panel(container)).toHaveAttribute('data-status', 'success');
		expect(screen.getByText('Complete')).toBeInTheDocument();
		expect(screen.getByText(/completed successfully/)).toBeInTheDocument();
		expect(get(backupRunning)).toBe(false);
	});

	it('renders a partial completion as a warning, never as success', async () => {
		mocks.getRun.mockResolvedValue({ id: 'r1', error_message: 'Backup completed with problems (backup path(s) not found: /x)' });
		const { container } = await renderIdlePanel();
		emit('backup:started', { run_id: 'r1' });
		emit('backup:completed', { run_id: 'r1', status: 'partial', total_bytes: 10 });
		await tick();

		expect(panel(container)).toHaveAttribute('data-status', 'partial');
		expect(screen.getByText('Completed with problems')).toBeInTheDocument();
		expect(screen.queryByText(/completed successfully/)).toBeNull();
		await waitFor(() => {
			expect(screen.getByText(/backup path\(s\) not found/)).toBeInTheDocument();
		});
		expect(screen.getByText('View run details')).toHaveAttribute('href', '/backups/r1');
	});

	it('renders a completed event with status failed as failed', async () => {
		const { container } = await renderIdlePanel();
		emit('backup:started', { run_id: 'r1' });
		emit('backup:completed', { run_id: 'r1', status: 'failed', total_bytes: 0 });
		await tick();

		expect(panel(container)).toHaveAttribute('data-status', 'failed');
		expect(screen.getByText('Failed')).toBeInTheDocument();
		expect(screen.queryByText('Complete')).toBeNull();
	});

	it('ignores terminal events for a different run while a run is active', async () => {
		const { container } = await renderIdlePanel();
		emit('backup:started', { run_id: 'active' });
		emit('backup:failed', { run_id: 'other', error: 'Another backup is already running', error_category: 'conflict' });
		emit('backup:cancelled', { run_id: 'other', status: 'skipped', error: 'lock held' });
		await tick();

		expect(panel(container)).toHaveAttribute('data-status', 'running');
		expect(get(backupRunning)).toBe(true);
	});

	it('renders a skipped run as informational when no run is active', async () => {
		const { container } = await renderIdlePanel();
		emit('backup:cancelled', { run_id: 'sched-1', status: 'skipped', error: 'Another backup held the lock' });
		await tick();

		expect(panel(container)).toHaveAttribute('data-status', 'skipped');
		expect(screen.getByText('Skipped')).toBeInTheDocument();
		expect(screen.getByText('Another backup held the lock')).toBeInTheDocument();
		expect(screen.queryByText('Failed')).toBeNull();
	});

	it('hides DB Dumps from the phase rail for flash jobs', async () => {
		mocks.getRun.mockResolvedValue({ id: 'r1', job_type: 'flash' });
		await renderIdlePanel();
		emit('backup:started', { run_id: 'r1' });

		await waitFor(() => {
			expect(screen.getByText('Flash Drive')).toBeInTheDocument();
		});
		expect(screen.queryByText('DB Dumps')).toBeNull();
		expect(screen.getByText('Flash')).toBeInTheDocument();
	});
});
