<script lang="ts">
	import { TriangleAlert } from 'lucide-svelte';
	import type { BackupFreshness } from '$lib/types';
	import { formatStaleJobLine, staleJobHref, staleJobs } from '$lib/utils/freshness';
	import { timeAgo } from '$lib/utils/date';

	export let freshness: BackupFreshness | null | undefined = null;

	$: jobs = staleJobs(freshness);
	// Recomputed whenever a new status payload arrives.
	$: lines = jobs.map((job) => ({ job, text: formatStaleJobLine(job, new Date()) }));
</script>

{#if freshness?.stale}
	<div role="alert" class="rounded-lg border border-danger bg-danger-bg px-4 py-3">
		<div class="flex items-start gap-3">
			<TriangleAlert class="w-5 h-5 text-danger shrink-0 mt-0.5" />
			<div class="min-w-0 flex-1">
				<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
					<p class="text-sm font-semibold text-danger">
						{jobs.length === 1 ? 'A backup job is overdue' : jobs.length > 1 ? `${jobs.length} backup jobs are overdue` : 'Backups are overdue'}
					</p>
					{#if freshness.checked_at}
						<span class="text-xs text-text-muted">Checked {timeAgo(freshness.checked_at)}</span>
					{/if}
				</div>
				<p class="mt-1 text-xs text-text-secondary">
					No good backup has been produced within the expected window. A restore right now would bring back older data.
				</p>
				{#if jobs.length > 0}
					<ul class="mt-2 space-y-1">
						{#each lines as { job, text } (job.job_id)}
							<li>
								<a
									href={staleJobHref(job)}
									class="group inline-flex items-center gap-1.5 text-sm text-text hover:text-danger transition-colors"
								>
									<span class="w-1.5 h-1.5 rounded-full bg-danger shrink-0"></span>
									<span>{text}</span>
									<span class="text-xs text-danger group-hover:text-danger">&rarr;</span>
								</a>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	</div>
{/if}
