<svelte:head>
	<title>Activity | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import StatusBadge from '$lib/components/shared/StatusBadge.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { timeAgo } from '$lib/utils/date';
	import { onMount } from 'svelte';

	const PAGE_SIZE = 50;
	let activities: any[] = [];
	let loading = true;
	let loadingMore = false;
	let total = 0;
	let offset = 0;
	let error = '';

	$: hasMore = offset < total;

	onMount(() => {
		(async () => {
			try {
				const result = await api.listActivity(PAGE_SIZE);
				activities = result.activities || [];
				total = result.total || 0;
				offset = activities.length;
			} catch (e: any) {
				console.error(e);
				error = e.message || 'Failed to load activity';
			}
			loading = false;
		})();
	});

	async function loadMore() {
		loadingMore = true;
		try {
			const result = await api.listActivity(PAGE_SIZE, offset);
			const newItems = result.activities || [];
			activities = [...activities, ...newItems];
			total = result.total || 0;
			offset += newItems.length;
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Failed to load more activity' });
		}
		loadingMore = false;
	}

	const dotColors: Record<string, string> = {
		info: 'bg-primary',
		success: 'bg-success',
		warning: 'bg-warning',
		error: 'bg-danger',
	};
</script>

<Header title="Activity" />

<main class="p-6">
	<div class="card">
		<div class="flex items-center justify-between mb-6">
			<h3 class="font-semibold text-text">Activity Log</h3>
			<span class="text-xs text-text-secondary">Showing {activities.length} of {total} entries</span>
		</div>

		{#if error}
			<div class="p-3 bg-danger-bg border border-danger/30 rounded text-danger text-sm mb-4">{error}</div>
		{/if}

		{#if loading}
			<div class="animate-skeleton space-y-4">
				{#each Array(8) as _}
					<div class="h-16 bg-bg-surface-hover rounded"></div>
				{/each}
			</div>
		{:else if activities.length === 0}
			<p class="text-sm text-text-secondary text-center py-8">No activity recorded yet.</p>
		{:else}
			<!-- Vertical timeline -->
			<div class="relative">
				<!-- Connector line along the left -->
				<div class="absolute left-[7px] top-2 bottom-2 w-px bg-border-muted" aria-hidden="true"></div>

				<div class="space-y-1">
					{#each activities as entry}
						<div class="relative flex items-start gap-4 pl-6 py-3 rounded-lg bg-bg-surface hover:bg-bg-surface-hover transition-colors">
							<!-- Dot -->
							<div class="absolute left-0 top-[18px] w-[9px] h-[9px] rounded-full shrink-0 border-2 border-bg-base {dotColors[entry.severity || entry.level] || 'bg-primary'}"></div>

							<!-- Content -->
							<div class="min-w-0 flex-1">
								<div class="flex items-center gap-2 flex-wrap">
									<StatusBadge status={entry.type} size="sm" />
									<span class="text-xs text-text-secondary">{entry.action}</span>
								</div>
								<p class="text-sm text-text mt-1">{entry.message}</p>
							</div>

							<span class="font-mono text-xs text-text-secondary shrink-0 pt-0.5">{timeAgo(entry.timestamp)}</span>
						</div>
					{/each}
				</div>
			</div>

			{#if hasMore}
				<div class="text-center pt-4 border-t border-border/50 mt-4">
					<button on:click={loadMore} disabled={loadingMore} class="btn-secondary text-sm disabled:opacity-50">
						{loadingMore ? 'Loading…' : `Load More (${total - activities.length} remaining)`}
					</button>
				</div>
			{/if}
		{/if}
	</div>
</main>
