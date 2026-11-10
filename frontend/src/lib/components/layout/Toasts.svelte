<script lang="ts">
	import { toasts } from '$lib/stores/app';
	import { cn } from '$lib/utils/cn';
</script>

<!-- Full width minus gutters on phones; bottom-right column from sm up.
     Opaque -bg tokens: a translucent toast's text contrast depends on
     whatever page content happens to be underneath. -->
<div class="fixed bottom-4 left-4 right-4 sm:left-auto z-50 flex flex-col items-stretch sm:items-end gap-2" aria-live="polite" role="status">
	{#each $toasts as toast (toast.id)}
		<div class={cn(
			'px-4 py-3 rounded-lg shadow-lg border text-sm sm:max-w-sm break-words animate-slide-in',
			toast.type === 'success' && 'bg-success-bg border-success/30 text-success',
			toast.type === 'error' && 'bg-danger-bg border-danger/30 text-danger',
			toast.type === 'warning' && 'bg-warning-bg border-warning/30 text-warning',
			toast.type === 'info' && 'bg-primary-bg border-primary/30 text-primary'
		)}>
			{toast.message}
		</div>
	{/each}
</div>

<style>
	@keyframes slide-in {
		from { transform: translateX(100%); opacity: 0; }
		to { transform: translateX(0); opacity: 1; }
	}
	.animate-slide-in {
		animation: slide-in 0.3s ease-out;
	}
</style>
