<svelte:head>
	<title>Backups | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import SegmentedTabs from '$lib/components/ui/SegmentedTabs.svelte';
	import FilterBar from '$lib/components/ui/FilterBar.svelte';
	import StatusBadge from '$lib/components/shared/StatusBadge.svelte';
	import { api, ApiError } from '$lib/api/client';
	import { addToast, backupRunning } from '$lib/stores/app';
	import { subscribe as sseSubscribe } from '$lib/stores/sse';
	import { formatBytes, formatDuration } from '$lib/utils/format';
	import { timeAgo } from '$lib/utils/date';
	import { jobTypeLabel } from '$lib/utils/job-types';
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';

	let jobs: any[] = [];
	let selectedJob: any = null;
	let runs: any[] = [];
	let loading = true;
	let runsLoading = false;
	let error = '';
	let runsError = '';

	let filterSearch = '';
	let filterValues: Record<string, string> = {};

	// The layout tracks the global "backup running" flag; here we only refresh
	// the visible data when a run starts or ends.
	async function refreshAfterRunEvent() {
		try {
			const result = await api.listJobs();
			jobs = result.items || result.jobs || [];
			if (selectedJob) {
				selectedJob = jobs.find(j => j.id === selectedJob.id) ?? selectedJob;
				await loadRuns(selectedJob, false);
			}
		} catch (e) {
			console.error('Failed to refresh backups after run event:', e);
		}
	}

	const unsubStarted = sseSubscribe('backup:started', refreshAfterRunEvent);
	const unsubCompleted = sseSubscribe('backup:completed', refreshAfterRunEvent);
	const unsubFailed = sseSubscribe('backup:failed', refreshAfterRunEvent);
	const unsubCancelled = sseSubscribe('backup:cancelled', refreshAfterRunEvent);

	onDestroy(() => {
		unsubStarted();
		unsubCompleted();
		unsubFailed();
		unsubCancelled();
	});

	onMount(() => {
		(async () => {
			try {
				const result = await api.listJobs();
				jobs = result.items || result.jobs || [];
				if (jobs.length > 0) {
					// Deep link from the dashboard's overdue-backups alert: /backups?job=<id>
					const requested = $page.url.searchParams.get('job');
					await selectJob(jobs.find(j => j.id === requested) ?? jobs[0]);
				}
			} catch (e: any) {
				console.error(e);
				error = e.message || 'Failed to load backup jobs';
			}
			loading = false;
		})();
	});

	async function loadRuns(job: any, showSkeleton: boolean) {
		if (showSkeleton) runsLoading = true;
		runsError = '';
		try {
			const result = await api.listRuns(job.id);
			if (selectedJob?.id === job.id) {
				runs = result.runs || result.items || [];
			}
		} catch (e: any) {
			console.error(e);
			runsError = e.message || 'Failed to load run history';
		}
		runsLoading = false;
	}

	async function selectJob(job: any) {
		selectedJob = job;
		await loadRuns(job, true);
	}

	let triggering = false;

	async function triggerBackup() {
		if (!selectedJob || triggering) return;
		triggering = true;
		try {
			backupRunning.set(true);
			const result = await api.triggerJob(selectedJob.id);
			addToast({ type: 'success', message: `${selectedJob.name} started (${result.run_id})` });
			await loadRuns(selectedJob, false);
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
			// 409 means another backup is already running, so keep the flag set.
			backupRunning.set(e instanceof ApiError && e.status === 409);
		} finally {
			triggering = false;
		}
	}

	$: tabs = jobs.map(j => ({ id: j.id, label: j.name }));
	$: activeTab = selectedJob?.id ?? '';

	function handleTabChange(e: CustomEvent) {
		const job = jobs.find(j => j.id === e.detail.tab);
		if (job) selectJob(job);
	}

	$: filteredRuns = runs.filter(run => {
		const statusMatch = !filterValues.status || run.status === filterValues.status;
		const searchMatch = !filterSearch || run.id?.toLowerCase().includes(filterSearch.toLowerCase());
		return statusMatch && searchMatch;
	});

	const spineClass: Record<string, string> = {
		success: 'border-l-success',
		running: 'border-l-info',
		failed: 'border-l-danger',
		warning: 'border-l-warning',
		partial: 'border-l-warning',
		cancelled: 'border-l-neutral',
		skipped: 'border-l-neutral',
	};
</script>

<Header title="Backups" />

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	{#if loading}
		<div class="card animate-skeleton h-12 rounded-lg"></div>
	{:else}
		<!-- Job selector using SegmentedTabs -->
		<div class="flex items-center gap-4 flex-wrap">
			{#if tabs.length > 0}
				<SegmentedTabs {tabs} bind:activeTab on:change={handleTabChange} />
			{/if}
			<button
				on:click={triggerBackup}
				disabled={!selectedJob || triggering || $backupRunning}
				class="btn-primary ml-auto disabled:opacity-50 disabled:cursor-not-allowed"
				aria-label="Trigger backup now"
			>
				{$backupRunning ? 'Backup Running…' : 'Backup Now'}
			</button>
		</div>

		<!-- Run history -->
		{#if selectedJob}
			<div class="card">
				<div class="flex items-center justify-between mb-4">
					<div>
						<h3 class="font-semibold text-text">Run History — {selectedJob.name}</h3>
						<p class="text-xs text-text-secondary mt-0.5">{jobTypeLabel(selectedJob.type)}</p>
					</div>
					{#if runs.length > 0}
						<span class="text-xs text-text-secondary">{filteredRuns.length} of {runs.length} runs</span>
					{/if}
				</div>

				{#if runsError}
					<div class="p-3 bg-danger-bg border border-danger/30 rounded text-danger text-sm mb-4">{runsError}</div>
				{/if}

				<FilterBar
					searchPlaceholder="Search by run ID…"
					bind:searchValue={filterSearch}
					bind:filterValues
					filters={[
						{
							key: 'status',
							label: 'Status',
							options: [
								{ value: 'success', label: 'Success' },
								{ value: 'running', label: 'Running' },
								{ value: 'failed', label: 'Failed' },
								{ value: 'cancelled', label: 'Cancelled' },
								{ value: 'partial', label: 'Partial' },
								{ value: 'skipped', label: 'Skipped' },
							]
						}
					]}
				/>

				{#if runsLoading}
					<div class="animate-skeleton space-y-2">
						{#each Array(3) as _}
							<div class="h-14 bg-bg-surface-hover rounded"></div>
						{/each}
					</div>
				{:else if filteredRuns.length === 0}
					<p class="text-sm text-text-secondary py-4 text-center">
						{runs.length === 0 ? 'No runs yet. Click "Backup Now" to start.' : 'No runs match the current filter.'}
					</p>
				{:else}
					<div class="space-y-1">
						{#each filteredRuns as run}
							<a
								href="/backups/{run.id}"
								class="flex items-center gap-4 px-4 py-3 rounded-lg bg-bg-surface hover:bg-bg-surface-hover/50 transition-colors border-l-[3px] {spineClass[run.status] || 'border-l-neutral'}"
								title={run.error_message || 'View run details'}
							>
								<span class="font-mono text-xs text-text-secondary w-20 shrink-0 truncate">{run.id}</span>
								<div class="w-24 shrink-0">
									<StatusBadge status={run.status} size="sm" />
								</div>
								<span class="text-xs text-text-secondary w-16 shrink-0 capitalize">{run.trigger}</span>
								<span class="font-mono text-xs text-text-secondary flex-1">{timeAgo(run.started_at)}</span>
								<span class="font-mono text-xs text-text-secondary w-20 shrink-0 text-right">
									{run.duration_seconds ? formatDuration(run.duration_seconds) : '—'}
								</span>
								<span class="text-xs text-text-secondary w-16 shrink-0 text-right">
									{run.databases_dumped ?? 0}/{run.databases_discovered ?? 0} dbs
								</span>
								<span class="font-mono text-xs text-text-secondary w-20 shrink-0 text-right">{formatBytes(run.total_size_bytes || 0)}</span>
							</a>
							{#if run.error_message && (run.status === 'partial' || run.status === 'failed')}
								<p class="px-4 pb-2 -mt-1 text-xs truncate {run.status === 'failed' ? 'text-danger' : 'text-warning'}">{run.error_message}</p>
							{/if}
						{/each}
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</main>
