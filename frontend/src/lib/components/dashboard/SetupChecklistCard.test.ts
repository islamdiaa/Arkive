import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const runDrill = vi.fn();
const dismissSetupChecklist = vi.fn();
const addToast = vi.fn();

vi.mock('$lib/api/drills', () => ({
	runDrill: (...args: unknown[]) => runDrill(...args),
}));

vi.mock('$lib/api/setup-checklist', () => ({
	dismissSetupChecklist: (...args: unknown[]) => dismissSetupChecklist(...args),
}));

vi.mock('$lib/stores/app', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/stores/app')>();
	return { ...actual, addToast: (...args: unknown[]) => addToast(...args) };
});

import SetupChecklistCard from './SetupChecklistCard.svelte';
import { api, ApiError } from '$lib/api/client';
import { normalizeChecklist, SETUP_STEP_IDS, type SetupChecklist } from '$lib/utils/setup-checklist';

type StepOverrides = Record<string, { state?: string; detail?: string | null; method?: string; completed_at?: string }>;

function checklist(overrides: StepOverrides = {}, dismissed = false): SetupChecklist {
	return normalizeChecklist({
		dismissed,
		dismissed_at: dismissed ? '2026-11-09T10:00:00Z' : null,
		steps: SETUP_STEP_IDS.map((id) => ({
			id,
			state: 'todo',
			detail: null,
			completed_at: null,
			...(id === 'restore_test' ? { method: 'drill' } : {}),
			...overrides[id],
		})),
	})!;
}

const allDone = () =>
	checklist(Object.fromEntries(SETUP_STEP_IDS.map((id) => [id, { state: 'done' }])) as StepOverrides);

function step(id: string): HTMLElement {
	return screen.getByTestId(`setup-step-${id}`);
}

describe('SetupChecklistCard', () => {
	let triggerVerification: ReturnType<typeof vi.spyOn>;

	beforeEach(() => {
		runDrill.mockReset();
		dismissSetupChecklist.mockReset();
		addToast.mockReset();
		localStorage.clear();
		triggerVerification = vi.spyOn(api, 'triggerVerification');
	});

	afterEach(() => {
		triggerVerification.mockRestore();
	});

	it('renders nothing without a checklist or once dismissed', () => {
		const empty = render(SetupChecklistCard, { props: { checklist: null } });
		expect(empty.queryByTestId('setup-checklist')).toBeNull();
		empty.unmount();

		const dismissed = render(SetupChecklistCard, { props: { checklist: checklist({}, true) } });
		expect(dismissed.queryByTestId('setup-checklist')).toBeNull();
	});

	it('shows progress, every step, and why a step needs attention', () => {
		render(SetupChecklistCard, {
			props: {
				checklist: checklist({
					first_backup: { state: 'done', completed_at: new Date().toISOString() },
					offsite_target: { state: 'done', detail: 'B2 Offsite' },
					recovery_kit: { state: 'attention', detail: 'The encryption password changed after the last kit was saved; save a new one.' },
				}),
			},
		});

		expect(screen.getByText('Finish protecting this server')).toBeInTheDocument();
		expect(screen.getByTestId('setup-checklist-progress')).toHaveTextContent('2 of 5 done');
		expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2');
		for (const id of SETUP_STEP_IDS) expect(step(id)).toBeInTheDocument();
		expect(step('first_backup')).toHaveAttribute('data-state', 'done');
		expect(step('offsite_target')).toHaveTextContent('B2 Offsite');
		expect(step('recovery_kit')).toHaveAttribute('data-state', 'attention');
		expect(step('recovery_kit')).toHaveTextContent('password changed');
		// No dismissing an unfinished checklist.
		expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
	});

	it('links page-based steps to their settings pages', () => {
		render(SetupChecklistCard, { props: { checklist: checklist() } });
		expect(screen.getByRole('link', { name: 'Add offsite target' })).toHaveAttribute('href', '/settings/targets');
		expect(screen.getByRole('link', { name: 'Add notification channel' })).toHaveAttribute('href', '/settings/notifications');
		expect(screen.getByRole('link', { name: 'Save recovery kit' })).toHaveAttribute('href', '/settings/recovery-kit');
	});

	it('runs the first backup through the dashboard handler', async () => {
		const onRunBackup = vi.fn();
		const { rerender } = render(SetupChecklistCard, { props: { checklist: checklist(), onRunBackup } });

		await fireEvent.click(screen.getByRole('button', { name: 'Run first backup now' }));
		expect(onRunBackup).toHaveBeenCalledTimes(1);

		await rerender({ checklist: checklist(), onRunBackup, backupRunning: true });
		expect(screen.getByRole('button', { name: 'Backup running...' })).toBeDisabled();
	});

	it('offers a retry when the last backup failed', () => {
		render(SetupChecklistCard, {
			props: {
				checklist: checklist({ first_backup: { state: 'attention', detail: 'The last backup failed: boom' } }),
				onRunBackup: vi.fn(),
			},
		});
		expect(screen.getByRole('button', { name: 'Run backup again' })).toBeEnabled();
		expect(step('first_backup')).toHaveTextContent('The last backup failed: boom');
	});

	it('keeps the restore test disabled until a backup exists', () => {
		render(SetupChecklistCard, { props: { checklist: checklist() } });
		expect(screen.getByRole('button', { name: 'Run restore test now' })).toBeDisabled();
		expect(step('restore_test')).toHaveTextContent('Available once the first backup has completed.');
	});

	it('starts a restore drill when the server has databases', async () => {
		runDrill.mockResolvedValue({ drill_id: 'd1', status: 'running' });
		const onChange = vi.fn();
		render(SetupChecklistCard, {
			props: { checklist: checklist({ first_backup: { state: 'done' } }), onChange },
		});

		await fireEvent.click(screen.getByRole('button', { name: 'Run restore test now' }));
		await waitFor(() => expect(runDrill).toHaveBeenCalledTimes(1));
		expect(triggerVerification).not.toHaveBeenCalled();
		await waitFor(() => expect(onChange).toHaveBeenCalled());
		expect(addToast).toHaveBeenCalledWith({ type: 'info', message: 'Restore drill started' });
	});

	it('falls back to a verification restore test without databases', async () => {
		triggerVerification.mockResolvedValue({ run_id: 'v1', status: 'started' });
		render(SetupChecklistCard, {
			props: {
				checklist: checklist({ first_backup: { state: 'done' }, restore_test: { method: 'verification' } }),
			},
		});

		expect(step('restore_test')).toHaveTextContent('No databases here to drill');
		await fireEvent.click(screen.getByRole('button', { name: 'Run restore test now' }));
		await waitFor(() => expect(triggerVerification).toHaveBeenCalledTimes(1));
		expect(runDrill).not.toHaveBeenCalled();
		expect(addToast).toHaveBeenCalledWith({ type: 'info', message: 'Restore test started' });
	});

	it('reports an already-running drill as info', async () => {
		runDrill.mockRejectedValue(new ApiError(409, 'A restore drill is already running'));
		render(SetupChecklistCard, { props: { checklist: checklist({ first_backup: { state: 'done' } }) } });

		await fireEvent.click(screen.getByRole('button', { name: 'Run restore test now' }));
		await waitFor(() =>
			expect(addToast).toHaveBeenCalledWith({ type: 'info', message: 'A restore drill is already running.' }),
		);
	});

	it('shows a running restore test as in progress', () => {
		render(SetupChecklistCard, {
			props: { checklist: checklist({ first_backup: { state: 'done' }, restore_test: { state: 'running' } }) },
		});
		expect(screen.getByRole('button', { name: 'Restore test running...' })).toBeDisabled();
	});

	it('can be dismissed once every step is done', async () => {
		dismissSetupChecklist.mockResolvedValue({ ...allDone(), dismissed: true });
		const onChange = vi.fn();
		render(SetupChecklistCard, { props: { checklist: allDone(), onChange } });

		expect(screen.getByText('This server is protected')).toBeInTheDocument();
		expect(screen.getByTestId('setup-checklist-progress')).toHaveTextContent('5 of 5 done');
		// Done steps have no actions left.
		expect(screen.queryByRole('link', { name: 'Add offsite target' })).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		await waitFor(() => expect(screen.queryByTestId('setup-checklist')).toBeNull());
		expect(dismissSetupChecklist).toHaveBeenCalledTimes(1);
		expect(onChange).toHaveBeenCalled();
	});

	it('stays visible when dismissing fails', async () => {
		dismissSetupChecklist.mockRejectedValue(new ApiError(409, 'Finish every step before dismissing the checklist'));
		render(SetupChecklistCard, { props: { checklist: allDone() } });

		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		await waitFor(() =>
			expect(addToast).toHaveBeenCalledWith({
				type: 'error',
				message: 'Finish every step before dismissing the checklist',
			}),
		);
		expect(screen.getByTestId('setup-checklist')).toBeInTheDocument();
	});

	it('collapses to the summary and remembers it', async () => {
		const first = render(SetupChecklistCard, { props: { checklist: checklist() } });
		await fireEvent.click(screen.getByRole('button', { name: 'Hide steps' }));
		expect(screen.queryByTestId('setup-step-first_backup')).toBeNull();
		expect(screen.getByTestId('setup-checklist-progress')).toHaveTextContent('0 of 5 done');
		expect(localStorage.getItem('arkive_setup_checklist_collapsed')).toBe('true');
		first.unmount();

		render(SetupChecklistCard, { props: { checklist: checklist() } });
		await waitFor(() => expect(screen.getByRole('button', { name: 'Show steps' })).toBeInTheDocument());
		expect(screen.queryByTestId('setup-step-first_backup')).toBeNull();
	});
});
