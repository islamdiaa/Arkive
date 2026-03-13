<script lang="ts">
	import { Activity } from 'lucide-svelte';

	export let hostname: string = '';
	export let systemStatus: string = 'unknown';
	export let coverageWarnings: string[] = [];
	export let onBackup: (() => void) | undefined = undefined;
	export let backupDisabled: boolean = false;

	const statusColors: Record<string, string> = {
		healthy: 'bg-success',
		degraded: 'bg-warning',
		error: 'bg-danger',
		unknown: 'bg-neutral',
	};

	const statusLabels: Record<string, string> = {
		healthy: 'All Systems Operational',
		degraded: 'Degraded Performance',
		error: 'System Error',
		unknown: 'Status Unknown',
	};

	$: dotColor = statusColors[systemStatus] ?? statusColors.unknown;
	$: statusLabel = statusLabels[systemStatus] ?? statusLabels.unknown;
</script>

<div class="flex items-center justify-between px-4 py-2.5 bg-bg-elevated border border-border rounded-lg">
	<div class="flex items-center gap-4">
		{#if hostname}
			<span class="text-sm font-medium text-text font-mono">{hostname}</span>
			<span class="w-px h-4 bg-border"></span>
		{/if}
		<div class="flex items-center gap-2">
			<span class="w-2 h-2 rounded-full shrink-0 {dotColor}"></span>
			<span class="text-sm text-text-secondary">{statusLabel}</span>
		</div>
	</div>
	<div class="flex items-center gap-2">
		<a href="/logs" class="btn-ghost btn-sm flex items-center gap-1.5">
			<Activity class="w-3.5 h-3.5" />
			View Logs
		</a>
		{#if onBackup}
			<button
				on:click={onBackup}
				disabled={backupDisabled}
				class="btn-primary btn-sm flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
			>
				Run Backup
			</button>
		{/if}
	</div>
</div>

{#if coverageWarnings.length > 0}
	<a href="/settings/directories" class="mt-3 rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 block hover:bg-warning/15 transition-colors group">
		<div class="flex items-center justify-between">
			<p class="text-sm font-medium text-warning">Backup coverage needs attention</p>
			<span class="text-xs text-warning/70 group-hover:text-warning transition-colors">Settings &rarr;</span>
		</div>
		<p class="mt-1 text-xs text-text-secondary">{coverageWarnings[0]}</p>
		{#if coverageWarnings.length > 1}
			<p class="mt-1 text-xs text-text-muted">+ {coverageWarnings.length - 1} more coverage warning{coverageWarnings.length > 2 ? 's' : ''}</p>
		{/if}
	</a>
{/if}
