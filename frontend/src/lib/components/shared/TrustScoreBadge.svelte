<script lang="ts">
	import { timeAgo } from '$lib/utils/date';

	export let score: number | null = null;
	export let lastVerifiedAt: string | null = null;
	export let verifying: boolean = false;

	$: hasScore = score !== null && score !== undefined;
	$: displayScore = hasScore ? Math.round(score!) : '--';
	$: color = !hasScore ? 'text-text-muted' :
		score! >= 80 ? 'text-success' :
		score! >= 50 ? 'text-warning' :
		'text-danger';
	$: ringColor = !hasScore ? 'stroke-border-muted' :
		score! >= 80 ? 'stroke-success' :
		score! >= 50 ? 'stroke-warning' :
		'stroke-danger';
	$: pct = hasScore ? Math.min(100, Math.max(0, score!)) / 100 : 0;

	// SVG circle math: radius=36, circumference=2*pi*36 ~= 226.19
	const bgRingColor = 'stroke-border-muted';
	const radius = 36;
	const circumference = 2 * Math.PI * radius;
	$: dashOffset = circumference * (1 - pct);
</script>

<div class="flex flex-col items-center gap-1.5">
	<div class="relative w-[88px] h-[88px]">
		<svg viewBox="0 0 80 80" class="w-full h-full -rotate-90">
			<!-- Background ring -->
			<circle
				cx="40" cy="40" r={radius}
				fill="none"
				class={bgRingColor}
				stroke-width="6"
				stroke-linecap="round"
			/>
			<!-- Score ring -->
			{#if hasScore && !verifying}
				<circle
					cx="40" cy="40" r={radius}
					fill="none"
					class={ringColor}
					stroke-width="6"
					stroke-linecap="round"
					stroke-dasharray={circumference}
					stroke-dashoffset={dashOffset}
					style="transition: stroke-dashoffset 0.6s ease-out;"
				/>
			{/if}
			{#if verifying}
				<circle
					cx="40" cy="40" r={radius}
					fill="none"
					class="stroke-primary animate-spin-slow"
					stroke-width="6"
					stroke-linecap="round"
					stroke-dasharray="{circumference * 0.25} {circumference * 0.75}"
				/>
			{/if}
		</svg>
		<div class="absolute inset-0 flex items-center justify-center">
			{#if verifying}
				<span class="text-xs text-text-secondary">Verifying</span>
			{:else}
				<span class="text-xl font-bold font-mono {color}">{displayScore}</span>
			{/if}
		</div>
	</div>
	<div class="text-center">
		{#if lastVerifiedAt}
			<p class="text-[11px] text-text-muted">Verified {timeAgo(lastVerifiedAt)}</p>
		{:else if !verifying}
			<p class="text-[11px] text-text-muted">Never verified</p>
		{/if}
	</div>
</div>

<style>
	.animate-spin-slow {
		animation: spin-slow 1.5s linear infinite;
		transform-origin: center;
	}
	@keyframes spin-slow {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
