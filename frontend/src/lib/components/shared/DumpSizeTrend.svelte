<script lang="ts">
	import type { DatabaseSizePoint } from '$lib/types';
	import { trendGeometry } from '$lib/utils/database-health';
	import { formatBytes } from '$lib/utils/format';
	import { formatDate } from '$lib/utils/date';

	/** Recent dump attempts, oldest first. */
	export let points: DatabaseSizePoint[] = [];
	export let width = 120;
	export let height = 28;

	$: geometry = trendGeometry(points, width, height);
	$: sized = (points ?? []).filter((p) => p.status === 'success' && typeof p.size_bytes === 'number');
	$: failedCount = (points ?? []).filter((p) => p.status === 'failed').length;
	$: label = sized.length
		? `Dump size over the last ${(points ?? []).length} runs: ${formatBytes(sized[0].size_bytes ?? 0)} to ${formatBytes(sized[sized.length - 1].size_bytes ?? 0)}${failedCount ? `, ${failedCount} failed` : ''}`
		: (points ?? []).length
			? `No successful dump in the last ${(points ?? []).length} runs`
			: 'No dump history yet';

	function dotTitle(dot: { status: string; size: number | null; at: string; shrunk: boolean }): string {
		const when = dot.at ? formatDate(dot.at) : '';
		if (dot.status !== 'success') return `${when}: dump ${dot.status}`;
		return `${when}: ${formatBytes(dot.size ?? 0)}${dot.shrunk ? ' (sharp shrink)' : ''}`;
	}
</script>

{#if (points ?? []).length}
	<svg
		{width}
		{height}
		viewBox="0 0 {width} {height}"
		role="img"
		aria-label={label}
		class="overflow-visible shrink-0"
		data-testid="dump-size-trend"
	>
		<title>{label}</title>
		{#if geometry.line}
			<polyline
				points={geometry.line}
				fill="none"
				stroke="currentColor"
				stroke-width="1.5"
				stroke-linejoin="round"
				stroke-linecap="round"
				class="text-primary"
			/>
		{/if}
		{#each geometry.dots as dot}
			{#if dot.status !== 'success'}
				<circle cx={dot.x} cy={dot.y} r="2.5" fill="currentColor" class="text-danger" data-status="failed">
					<title>{dotTitle(dot)}</title>
				</circle>
			{:else if dot.shrunk}
				<circle cx={dot.x} cy={dot.y} r="3" fill="currentColor" class="text-warning" data-status="shrunk">
					<title>{dotTitle(dot)}</title>
				</circle>
			{:else}
				<circle cx={dot.x} cy={dot.y} r="1.5" fill="currentColor" class="text-primary" data-status="success">
					<title>{dotTitle(dot)}</title>
				</circle>
			{/if}
		{/each}
	</svg>
{:else}
	<span class="text-xs text-text-muted" data-testid="dump-size-trend-empty">{label}</span>
{/if}
