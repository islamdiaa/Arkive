<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ChevronRight, FlaskConical } from 'lucide-svelte';
	import StatusBadge from '$lib/components/shared/StatusBadge.svelte';
	import { getDrill, listDrills, runDrill } from '$lib/api/drills';
	import { ApiError } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { subscribe as sseSubscribe } from '$lib/stores/sse';
	import { formatBytes, formatDuration } from '$lib/utils/format';
	import { formatDate, timeAgo } from '$lib/utils/date';
	import {
		drillCompletedToast,
		drillCountsLabel,
		hasRunningDrill,
		runDrillErrorMessage,
	} from '$lib/utils/drills';
	import type { RestoreDrill, RestoreDrillDetail } from '$lib/types';

	/** How many recent drills to list. */
	export let limit: number = 5;
	/** Called when a drill starts or finishes, so the parent can refresh status. */
	export let onChange: (() => void) | undefined = undefined;

	let drills: RestoreDrill[] = [];
	let total = 0;
	let loading = true;
	let error = '';
	let starting = false;
	/** Drill id returned by POST /drills/run until it shows up as finished. */
	let awaitingId: string | null = null;

	let expandedId: string | null = null;
	let details: Record<string, RestoreDrillDetail> = {};
	let detailLoadingId: string | null = null;
	let detailError = '';

	$: running = starting || awaitingId !== null || hasRunningDrill(drills);

	// Fallback for a missed drill:completed event (SSE reconnecting): poll the
	// list while a drill is running.
	const POLL_MS = 15000;
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	$: if (running && !pollTimer) {
		pollTimer = setInterval(load, POLL_MS);
	} else if (!running && pollTimer) {
		clearInterval(pollTimer);
		pollTimer = null;
	}

	async function load() {
		try {
			const result = await listDrills(limit, 0);
			drills = result.items;
			total = result.total;
			error = '';
			if (awaitingId) {
				const tracked = drills.find((d) => d.id === awaitingId);
				if (tracked && tracked.status !== 'running') {
					awaitingId = null;
					onChange?.();
				}
			}
		} catch (e: any) {
			error = e?.message || 'Failed to load restore drills';
		} finally {
			loading = false;
		}
	}

	async function loadDetail(id: string) {
		detailLoadingId = id;
		detailError = '';
		try {
			const detail = await getDrill(id);
			details = { ...details, [id]: detail };
		} catch (e: any) {
			detailError = e?.message || 'Failed to load drill details';
		} finally {
			if (detailLoadingId === id) detailLoadingId = null;
		}
	}

	function toggle(drill: RestoreDrill) {
		if (expandedId === drill.id) {
			expandedId = null;
			return;
		}
		expandedId = drill.id;
		// Running drills have no final results yet; always refetch those.
		if (!details[drill.id] || drill.status === 'running') loadDetail(drill.id);
	}

	async function handleRun() {
		if (running) return;
		starting = true;
		try {
			const result = await runDrill();
			awaitingId = result?.drill_id ?? null;
			addToast({ type: 'info', message: 'Restore drill started' });
			onChange?.();
			await load();
		} catch (e: any) {
			const status = e instanceof ApiError ? e.status : undefined;
			const { tone, message } = runDrillErrorMessage(status, e?.message);
			addToast({ type: tone, message });
			if (status === 409) await load();
		} finally {
			starting = false;
		}
	}

	const unsubStarted = sseSubscribe('drill:started', (event) => {
		const id = event.data?.drill_id;
		if (typeof id === 'string' && !awaitingId) awaitingId = id;
		load();
		onChange?.();
	});

	const unsubCompleted = sseSubscribe('drill:completed', (event) => {
		const id = typeof event.data?.drill_id === 'string' ? (event.data.drill_id as string) : null;
		if (!id || id === awaitingId) awaitingId = null;
		addToast(drillCompletedToast(event.data ?? {}));
		load();
		if (id && expandedId === id) loadDetail(id);
		else if (id && details[id]) {
			// Drop the cached (now stale) results so the next expand refetches.
			const next = { ...details };
			delete next[id];
			details = next;
		}
		onChange?.();
	});

	onMount(load);

	onDestroy(() => {
		unsubStarted();
		unsubCompleted();
		if (pollTimer) clearInterval(pollTimer);
		pollTimer = null;
	});

	function seconds(value: number | null | undefined): string {
		if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
		if (value < 1) return '<1s';
		return formatDuration(Math.round(value));
	}
</script>

<div class="bg-bg-surface border border-border rounded-lg p-5" data-testid="restore-drills-panel">
	<div class="flex flex-wrap items-start justify-between gap-3 mb-4">
		<div class="min-w-0">
			<div class="flex items-center gap-2">
				<FlaskConical class="w-4 h-4 text-text-secondary" />
				<h3 class="font-semibold text-text">Restore Drills</h3>
			</div>
			<p class="mt-1 text-xs text-text-secondary max-w-2xl">
				A drill restores recent database dumps from the latest snapshot into a temporary, network-isolated copy of each database's own container image, proving the dumps actually restore.
			</p>
		</div>
		<div class="flex items-center gap-2">
			<a href="/settings/general#restore-drills" class="btn-ghost btn-sm">Settings</a>
			<button
				class="btn-primary btn-sm disabled:opacity-50 disabled:cursor-not-allowed"
				on:click={handleRun}
				disabled={running}
			>
				{running ? 'Drill running...' : 'Run drill now'}
			</button>
		</div>
	</div>

	{#if loading}
		<div class="space-y-2">
			{#each Array(2) as _}
				<div class="h-10 rounded bg-bg-elevated animate-skeleton"></div>
			{/each}
		</div>
	{:else if error}
		<p class="text-sm text-danger">Could not load restore drills: {error}</p>
	{:else if drills.length === 0}
		<div class="rounded-lg border border-dashed border-border-muted bg-bg-elevated p-4">
			<p class="text-sm text-text-secondary">No restore drills yet.</p>
			<p class="text-xs text-text-muted mt-1">Run one now, or wait for the scheduled drill, to prove your database dumps restore.</p>
		</div>
	{:else}
		<div class="divide-y divide-border border border-border rounded-md">
			{#each drills as drill (drill.id)}
				{@const expanded = expandedId === drill.id}
				<div>
					<button
						type="button"
						class="w-full flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2.5 text-left hover:bg-bg-surface-hover transition-colors"
						aria-expanded={expanded}
						aria-controls="drill-detail-{drill.id}"
						on:click={() => toggle(drill)}
					>
						<ChevronRight class="w-3.5 h-3.5 text-text-muted shrink-0 transition-transform {expanded ? 'rotate-90' : ''}" />
						<StatusBadge status={drill.status} size="sm" />
						<span class="text-sm text-text" title={drill.started_at ? formatDate(drill.started_at) : ''}>
							{drill.started_at ? timeAgo(drill.started_at) : '—'}
						</span>
						<span class="text-xs text-text-secondary">{drill.status === 'running' ? 'in progress' : seconds(drill.duration_seconds)}</span>
						<span class="text-xs text-text-secondary">{drillCountsLabel(drill)}</span>
						<span class="ml-auto text-[11px] text-text-muted">
							{drill.trigger}{drill.target_name ? ` · ${drill.target_name}` : ''}
						</span>
					</button>
					{#if expanded}
						<div id="drill-detail-{drill.id}" class="px-3 pb-3 pt-1 bg-bg-elevated">
							{#if drill.error_message}
								<p class="mb-2 rounded border border-danger-muted bg-danger-bg px-3 py-2 text-xs text-danger">{drill.error_message}</p>
							{/if}
							{#if drill.snapshot_id}
								<p class="mb-2 text-[11px] text-text-muted">Snapshot <span class="font-mono">{drill.snapshot_id.slice(0, 8)}</span>{drill.started_at ? ` · started ${formatDate(drill.started_at)}` : ''}</p>
							{/if}
							{#if detailLoadingId === drill.id && !details[drill.id]}
								<p class="text-xs text-text-secondary">Loading results...</p>
							{:else if detailError && !details[drill.id]}
								<p class="text-xs text-danger">{detailError}</p>
							{:else if details[drill.id]?.results?.length}
								<div class="overflow-x-auto">
									<table class="w-full text-xs">
										<thead>
											<tr class="text-left text-text-muted uppercase tracking-wide">
												<th class="py-1.5 pr-3 font-medium">Database</th>
												<th class="py-1.5 pr-3 font-medium">Type</th>
												<th class="py-1.5 pr-3 font-medium">Dump</th>
												<th class="py-1.5 pr-3 font-medium">Result</th>
												<th class="py-1.5 pr-3 font-medium">Time</th>
												<th class="py-1.5 font-medium">Detail</th>
											</tr>
										</thead>
										<tbody>
											{#each details[drill.id].results as result, i (i)}
												<tr class="border-t border-border-muted align-top">
													<td class="py-1.5 pr-3">
														<span class="text-text font-medium">{result.db_name}</span>
														<span class="block text-text-muted">{result.container_name}</span>
													</td>
													<td class="py-1.5 pr-3 text-text-secondary">{result.db_type}</td>
													<td class="py-1.5 pr-3 text-text-secondary whitespace-nowrap">
														{typeof result.dump_size_bytes === 'number' ? formatBytes(result.dump_size_bytes) : '—'}
													</td>
													<td class="py-1.5 pr-3"><StatusBadge status={result.status} size="sm" /></td>
													<td class="py-1.5 pr-3 text-text-secondary whitespace-nowrap">{seconds(result.duration_seconds)}</td>
													<td class="py-1.5 text-text-secondary break-words {result.status === 'failed' ? 'text-danger font-mono' : ''}">{result.detail || '—'}</td>
												</tr>
											{/each}
										</tbody>
									</table>
								</div>
							{:else if drill.status === 'running'}
								<p class="text-xs text-text-secondary">Drill in progress — results appear when it finishes.</p>
							{:else}
								<p class="text-xs text-text-secondary">No per-database results were recorded for this drill.</p>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
		{#if total > drills.length}
			<p class="mt-2 text-[11px] text-text-muted">Showing the {drills.length} most recent of {total} drills.</p>
		{/if}
	{/if}
</div>
