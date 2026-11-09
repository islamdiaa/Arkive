<script lang="ts">
	import { CloudOff } from 'lucide-svelte';
	import type { OffsiteStatus } from '$lib/types';

	/** `offsite` from /api/status. Shown only when the backend says every target the jobs use is on this server. */
	export let offsite: OffsiteStatus | null | undefined = undefined;

	$: targets = offsite?.local_targets ?? [];
	// An offsite target exists but no enabled job uploads to it: adding another won't help.
	$: unused = offsite?.unused_offsite_targets ?? [];
	$: unusedNames = unused.map((t) => t.name).join(', ');
</script>

{#if offsite?.warning === true}
	<div class="rounded-lg border border-warning bg-warning-bg px-4 py-3" data-testid="offsite-warning">
		<div class="flex items-start gap-3">
			<CloudOff class="w-5 h-5 text-warning shrink-0 mt-0.5" />
			<div class="min-w-0 flex-1">
				<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
					<p class="text-sm font-semibold text-warning">No offsite copy</p>
					{#if unused.length > 0}
						<a
							href="/settings/jobs"
							class="text-xs text-warning hover:underline whitespace-nowrap"
							data-testid="offsite-review-jobs"
						>
							Review backup jobs &rarr;
						</a>
					{:else}
						<a
							href="/settings/targets?add=1"
							class="text-xs text-warning hover:underline whitespace-nowrap"
							data-testid="offsite-add-target"
						>
							Add an offsite target &rarr;
						</a>
					{/if}
				</div>
				<p class="mt-1 text-xs text-text-secondary">
					Every backup is stored on this server. A disk failure, fire, theft or ransomware here would take the backups with it — keep one copy on another machine or in the cloud.
				</p>
				{#if unused.length > 0}
					<p class="mt-1 text-xs text-text-secondary" data-testid="offsite-unused">
						<span class="font-medium">{unusedNames}</span>
						{unused.length === 1 ? 'is' : 'are'} enabled, but no enabled backup job uploads to {unused.length === 1 ? 'it' : 'them'}.
						A job created in Settings &gt; Jobs uploads to every enabled target.
					</p>
				{/if}
				{#if targets.length > 0}
					<ul class="mt-2 space-y-0.5">
						{#each targets as target (target.id)}
							<li class="text-xs text-text-muted">
								<span class="text-text-secondary">{target.name}</span> — {target.reason}
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	</div>
{/if}
