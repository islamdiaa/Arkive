<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { NotificationEventCatalog } from '$lib/types';
	import { WILDCARD_EVENT, toggleEventValue } from '$lib/utils/notification-events';

	/** Event catalog from GET /api/notifications/events (or the built-in fallback). */
	export let catalog: NotificationEventCatalog;
	/** Selected event values (bindable). */
	export let selected: string[] = [];
	/** Legacy values removed from the channel; listed so the user knows. */
	export let dropped: string[] = [];
	/** True when the catalog is the built-in copy because the request failed. */
	export let fromFallback: boolean = false;
	export let idPrefix: string = 'events';
	export let label: string = 'Events';
	export let compact: boolean = false;

	const dispatch = createEventDispatcher<{ change: string[] }>();

	function toggle(value: string) {
		selected = toggleEventValue(selected, value);
		dispatch('change', selected);
	}

	$: wildcard = selected.includes(WILDCARD_EVENT);
</script>

<div class="space-y-2">
	<span id="{idPrefix}-label" class="block text-sm text-text-secondary">{label}</span>
	<div class="grid gap-1.5 {compact ? 'sm:grid-cols-2' : ''}" role="group" aria-labelledby="{idPrefix}-label">
		{#if wildcard}
			<button
				type="button"
				class="flex items-start gap-2.5 rounded-md border border-primary bg-primary-bg px-3 py-2 text-left text-sm text-text transition-colors"
				aria-pressed="true"
				on:click={() => toggle(WILDCARD_EVENT)}
			>
				<span class="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border border-primary bg-primary">
					<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
				</span>
				<span class="min-w-0">
					<span class="font-medium">All events</span>
					<span class="block text-xs text-text-muted">Every event Arkive sends, including ones added later</span>
				</span>
			</button>
		{/if}
		{#each catalog.events as event (event.value)}
			{@const on = selected.includes(event.value)}
			<button
				type="button"
				class="flex items-start gap-2.5 rounded-md border px-3 py-2 text-left text-sm transition-colors
					{on ? 'border-primary bg-primary-bg text-text' : 'border-border bg-bg-input text-text-secondary hover:border-border-strong'}"
				aria-pressed={on}
				title={event.value}
				on:click={() => toggle(event.value)}
			>
				<span class="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border {on ? 'border-primary bg-primary' : 'border-border'}">
					{#if on}
						<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
					{/if}
				</span>
				<span class="min-w-0">
					<span class="font-medium">{event.label}</span>
					{#if event.description}
						<span class="block text-xs text-text-muted">{event.description}</span>
					{/if}
				</span>
			</button>
		{/each}
	</div>
	{#if dropped.length > 0}
		<div class="rounded-md border border-warning-muted bg-warning-bg px-3 py-2 text-xs text-text-secondary" role="status" data-testid="dropped-events">
			<span class="font-medium text-warning">Unsupported events removed:</span>
			<span class="font-mono">{dropped.join(', ')}</span>.
			Arkive no longer sends these, so they will be dropped when you save.
		</div>
	{/if}
	{#if fromFallback}
		<p class="text-[11px] text-text-muted">Couldn't load the event list from the server; showing the built-in list.</p>
	{/if}
</div>
