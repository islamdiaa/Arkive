import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const listDrills = vi.fn();
const getDrill = vi.fn();
const runDrill = vi.fn();
const addToast = vi.fn();

vi.mock('$lib/api/drills', () => ({
	listDrills: (...args: unknown[]) => listDrills(...args),
	getDrill: (...args: unknown[]) => getDrill(...args),
	runDrill: (...args: unknown[]) => runDrill(...args),
}));

vi.mock('$lib/stores/app', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/stores/app')>();
	return { ...actual, addToast: (...args: unknown[]) => addToast(...args) };
});

import RestoreDrillsPanel from './RestoreDrillsPanel.svelte';
import { ApiError } from '$lib/api/client';

const failedDrill = {
	id: 'd2',
	trigger: 'scheduled',
	status: 'failed',
	target_id: 't1',
	target_name: 'B2 Primary',
	snapshot_id: 'def67890aa',
	started_at: new Date(Date.now() - 3600000).toISOString(),
	completed_at: new Date(Date.now() - 3500000).toISOString(),
	duration_seconds: 212,
	databases_tested: 2,
	databases_passed: 1,
	databases_failed: 1,
	error_message: '1 of 2 databases failed to restore',
};

describe('RestoreDrillsPanel', () => {
	beforeEach(() => {
		listDrills.mockReset();
		getDrill.mockReset();
		runDrill.mockReset();
		addToast.mockReset();
	});

	it('shows an empty state when no drills have run', async () => {
		listDrills.mockResolvedValue({ items: [], total: 0 });
		render(RestoreDrillsPanel);
		expect(await screen.findByText('No restore drills yet.')).toBeInTheDocument();
	});

	it('lists drills and expands per-database results', async () => {
		listDrills.mockResolvedValue({ items: [failedDrill], total: 1 });
		getDrill.mockResolvedValue({
			...failedDrill,
			results: [
				{ container_name: 'immich-postgres', db_type: 'postgres', db_name: 'immich', dump_file: 'a.sql', dump_size_bytes: 1048576, status: 'passed', detail: 'Restored 42 tables', duration_seconds: 12.4 },
				{ container_name: 'paperless-db', db_type: 'postgres', db_name: 'paperless', dump_file: 'b.sql', dump_size_bytes: 2048, status: 'failed', detail: 'psql: ERROR: syntax error', duration_seconds: 0.4 },
			],
		});
		render(RestoreDrillsPanel);

		expect(await screen.findByText('2 tested · 1 passed · 1 failed')).toBeInTheDocument();
		const row = screen.getByText('2 tested · 1 passed · 1 failed').closest('button')!;
		expect(row).toHaveAttribute('aria-expanded', 'false');

		await fireEvent.click(row);
		expect(row).toHaveAttribute('aria-expanded', 'true');
		expect(getDrill).toHaveBeenCalledWith('d2');
		expect(await screen.findByText('psql: ERROR: syntax error')).toBeInTheDocument();
		expect(screen.getByText('1 of 2 databases failed to restore')).toBeInTheDocument();
		expect(screen.getByText('immich')).toBeInTheDocument();
	});

	it('reports "already running" (409) as info and keeps the list', async () => {
		listDrills.mockResolvedValue({ items: [failedDrill], total: 1 });
		runDrill.mockRejectedValue(new ApiError(409, 'A restore drill is already running'));
		render(RestoreDrillsPanel);

		const button = await screen.findByRole('button', { name: 'Run drill now' });
		await fireEvent.click(button);
		await waitFor(() =>
			expect(addToast).toHaveBeenCalledWith({ type: 'info', message: 'A restore drill is already running.' }),
		);
	});

	it('explains 503 when drills cannot run', async () => {
		listDrills.mockResolvedValue({ items: [], total: 0 });
		runDrill.mockRejectedValue(new ApiError(503, 'Docker is not available'));
		render(RestoreDrillsPanel);

		await fireEvent.click(await screen.findByRole('button', { name: 'Run drill now' }));
		await waitFor(() =>
			expect(addToast).toHaveBeenCalledWith({
				type: 'error',
				message: 'Restore drills are unavailable right now: Docker is not available',
			}),
		);
	});

	it('disables the run button while a drill is running', async () => {
		listDrills.mockResolvedValue({ items: [{ ...failedDrill, id: 'd3', status: 'running', completed_at: null }], total: 1 });
		render(RestoreDrillsPanel);
		const button = await screen.findByRole('button', { name: 'Drill running...' });
		expect(button).toBeDisabled();
	});
});
