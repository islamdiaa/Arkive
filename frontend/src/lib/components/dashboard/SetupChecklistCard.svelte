<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ChevronDown, Circle, CircleAlert, CircleCheck, LoaderCircle, ShieldCheck } from 'lucide-svelte';
	import { api, ApiError } from '$lib/api/client';
	import { runDrill } from '$lib/api/drills';
	import { dismissSetupChecklist } from '$lib/api/setup-checklist';
	import { addToast } from '$lib/stores/app';
	import { subscribe as sseSubscribe } from '$lib/stores/sse';
	import { timeAgo } from '$lib/utils/date';
	import { runDrillErrorMessage } from '$lib/utils/drills';
	import {
		checklistVisible,
		progressLabel,
		progressPercent,
		restoreTestBlocker,
		SETUP_STEP_LINKS,
		stepDescription,
		stepTitle,
		type SetupChecklist,
		type SetupChecklistStep,
		type SetupStepId,
	} from '$lib/utils/setup-checklist';

	/** GET /api/setup-checklist; the card renders nothing while this is null. */
	export let checklist: SetupChecklist | null | undefined = null;
	/** Starts the primary backup job (the dashboard's own "Run Backup" handler). */
	export let onRunBackup: (() => void) | undefined = undefined;
	/** True while any backup is running. */
	export let backupRunning: boolean = false;
	/** Called after an action so the parent re-fetches the checklist. */
	export let onChange: (() => void) | undefined = undefined;
	/** Steps hidden behind the summary line. Remembered per browser. */
	export let collapsed: boolean = false;

	const COLLAPSED_KEY = 'arkive_setup_checklist_collapsed';

	let startingRestore = false;
	let dismissing = false;
	let dismissedHere = false;

	$: visible = checklistVisible(checklist) && !dismissedHere;
	$: complete = checklist?.complete === true;
	$: blocker = restoreTestBlocker(checklist);

	onMount(() => {
		try {
			collapsed = localStorage.getItem(COLLAPSED_KEY) === 'true';
		} catch {
			// Storage blocked (private mode): just start expanded.
		}
	});

	function toggle() {
		collapsed = !collapsed;
		try {
			localStorage.setItem(COLLAPSED_KEY, String(collapsed));
		} catch {
			// Not remembered; harmless.
		}
	}

	async function runRestoreTest(step: SetupChecklistStep) {
		if (startingRestore) return;
		startingRestore = true;
		const verification = step.method === 'verification';
		try {
			if (verification) {
				await api.triggerVerification();
				addToast({ type: 'info', message: 'Restore test started' });
			} else {
				await runDrill();
				addToast({ type: 'info', message: 'Restore drill started' });
			}
			onChange?.();
		} catch (e: any) {
			const status = e instanceof ApiError ? e.status : undefined;
			if (verification) {
				// 409: a backup is running; 429: a verification ran minutes ago.
				addToast({
					type: status === 409 || status === 429 ? 'info' : 'error',
					message: e?.message || 'Failed to start the restore test',
				});
			} else {
				const { tone, message } = runDrillErrorMessage(status, e?.message);
				addToast({ type: tone, message });
			}
			if (status === 409) onChange?.();
		} finally {
			startingRestore = false;
		}
	}

	async function dismiss() {
		if (dismissing) return;
		dismissing = true;
		try {
			await dismissSetupChecklist();
			dismissedHere = true;
			addToast({ type: 'success', message: 'Setup checklist complete' });
			onChange?.();
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Could not dismiss the checklist' });
			onChange?.();
		} finally {
			dismissing = false;
		}
	}

	// Restore tests finish in the background; refresh when they do.
	const unsubscribers = [
		sseSubscribe('drill:completed', () => onChange?.()),
		sseSubscribe('verification:completed', () => onChange?.()),
		sseSubscribe('verification:failed', () => onChange?.()),
	];
	onDestroy(() => unsubscribers.forEach((unsub) => unsub()));

	function link(id: string) {
		return SETUP_STEP_LINKS[id as SetupStepId];
	}

	const stateLabels: Record<string, string> = {
		done: 'Done',
		todo: 'To do',
		running: 'In progress',
		attention: 'Needs attention',
	};
</script>

{#if visible && checklist}
	<section
		class="bg-bg-surface border rounded-lg p-5 {complete ? 'border-success' : 'border-border'}"
		data-testid="setup-checklist"
		aria-labelledby="setup-checklist-title"
	>
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div class="min-w-0">
				<div class="flex items-center gap-2">
					<ShieldCheck class="w-4 h-4 shrink-0 {complete ? 'text-success' : 'text-primary'}" />
					<h3 id="setup-checklist-title" class="font-semibold text-text">
						{complete ? 'This server is protected' : 'Finish protecting this server'}
					</h3>
				</div>
				<p class="mt-1 text-xs text-text-secondary max-w-2xl">
					{complete
						? 'A backup ran, a restore was proven, a copy lives off this server, failures reach you, and the recovery kit is saved.'
						: 'Setup is done, but a backup only protects you once it has run, restores, lives somewhere else, and you can open it without this server.'}
				</p>
			</div>
			<div class="flex items-center gap-2">
				<span class="text-xs text-text-secondary whitespace-nowrap" data-testid="setup-checklist-progress">
					{progressLabel(checklist)}
				</span>
				<button
					type="button"
					class="btn-ghost btn-sm flex items-center gap-1"
					aria-expanded={!collapsed}
					aria-controls="setup-checklist-steps"
					on:click={toggle}
				>
					<ChevronDown class="w-3.5 h-3.5 transition-transform {collapsed ? '-rotate-90' : ''}" />
					{collapsed ? 'Show steps' : 'Hide steps'}
				</button>
				{#if complete}
					<button
						type="button"
						class="btn-primary btn-sm disabled:opacity-50 disabled:cursor-not-allowed"
						on:click={dismiss}
						disabled={dismissing}
					>
						{dismissing ? 'Dismissing...' : 'Dismiss'}
					</button>
				{/if}
			</div>
		</div>

		<div
			class="mt-3 h-1.5 rounded-full bg-bg-elevated overflow-hidden"
			role="progressbar"
			aria-label="Setup checklist progress"
			aria-valuemin={0}
			aria-valuemax={checklist.total}
			aria-valuenow={checklist.completed_count}
		>
			<div
				class="h-full rounded-full transition-all {complete ? 'bg-success' : 'bg-primary'}"
				style="width: {progressPercent(checklist)}%"
			></div>
		</div>

		{#if !collapsed}
			<ol id="setup-checklist-steps" class="mt-4 divide-y divide-border border border-border rounded-md">
				{#each checklist.steps as step (step.id)}
					{@const stepLink = link(step.id)}
					<li
						class="flex items-start gap-3 px-3 py-3"
						data-testid="setup-step-{step.id}"
						data-state={step.state}
					>
						<span class="mt-0.5 shrink-0" title={stateLabels[step.state] ?? step.state}>
							{#if step.state === 'done'}
								<CircleCheck class="w-4 h-4 text-success" />
							{:else if step.state === 'running'}
								<LoaderCircle class="w-4 h-4 text-primary animate-spin" />
							{:else if step.state === 'attention'}
								<CircleAlert class="w-4 h-4 text-warning" />
							{:else}
								<Circle class="w-4 h-4 text-text-muted" />
							{/if}
							<span class="sr-only">{stateLabels[step.state] ?? step.state}:</span>
						</span>
						<!-- The action wraps under the text on narrow screens. -->
						<div class="min-w-0 flex-1 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
							<div class="min-w-0 flex-1 basis-56">
								<p class="text-sm font-medium {step.done ? 'text-text-secondary' : 'text-text'}">{stepTitle(step.id)}</p>
								{#if step.done}
									<p class="mt-0.5 text-xs text-text-muted">
										{step.detail ?? 'Completed'}{step.completed_at ? ` · ${timeAgo(step.completed_at)}` : ''}
									</p>
								{:else}
									<p class="mt-0.5 text-xs text-text-secondary">{stepDescription(step)}</p>
									{#if step.detail}
										<p class="mt-1 text-xs break-words {step.state === 'attention' ? 'text-warning' : 'text-text-muted'}">
											{step.detail}
										</p>
									{/if}
									{#if step.id === 'restore_test' && blocker && step.state !== 'running'}
										<p class="mt-1 text-[11px] text-text-muted">{blocker}</p>
									{/if}
								{/if}
							</div>
							{#if !step.done}
								<div class="shrink-0">
									{#if step.id === 'first_backup' && onRunBackup}
										<button
											type="button"
											class="btn-primary btn-sm disabled:opacity-50 disabled:cursor-not-allowed"
											on:click={onRunBackup}
											disabled={backupRunning || step.state === 'running'}
										>
											{backupRunning || step.state === 'running'
												? 'Backup running...'
												: step.state === 'attention'
													? 'Run backup again'
													: 'Run first backup now'}
										</button>
									{:else if step.id === 'restore_test'}
										<button
											type="button"
											class="btn-primary btn-sm disabled:opacity-50 disabled:cursor-not-allowed"
											on:click={() => runRestoreTest(step)}
											disabled={startingRestore || step.state === 'running' || blocker !== null || backupRunning}
											title={backupRunning && !blocker ? 'Available when the running backup finishes' : undefined}
										>
											{startingRestore
												? 'Starting...'
												: step.state === 'running'
													? 'Restore test running...'
													: 'Run restore test now'}
										</button>
									{:else if stepLink}
										<a href={stepLink.href} class="btn-secondary btn-sm inline-flex items-center">{stepLink.label}</a>
									{/if}
								</div>
							{/if}
						</div>
					</li>
				{/each}
			</ol>
		{/if}
	</section>
{/if}
