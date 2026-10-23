<script lang="ts">
	import { createEventDispatcher, tick } from 'svelte';
	import { goto } from '$app/navigation';

	export let open: boolean = false;

	const dispatch = createEventDispatcher<{ close: void }>();

	const routes = [
		{ label: 'Dashboard', href: '/' },
		{ label: 'Backups', href: '/backups' },
		{ label: 'Snapshots', href: '/snapshots' },
		{ label: 'Databases', href: '/databases' },
		{ label: 'Restore', href: '/restore' },
		{ label: 'Activity', href: '/activity' },
		{ label: 'Logs', href: '/logs' },
		{ label: 'Jobs', href: '/settings/jobs' },
		{ label: 'Settings — General', href: '/settings/general' },
		{ label: 'Settings — Targets', href: '/settings/targets' },
		{ label: 'Settings — Schedule', href: '/settings/schedule' },
		{ label: 'Settings — Directories', href: '/settings/directories' },
		{ label: 'Settings — Notifications', href: '/settings/notifications' },
		{ label: 'Settings — Monitoring', href: '/settings/monitoring' },
		{ label: 'Settings — Security', href: '/settings/security' },
		{ label: 'Settings — Recovery Kit', href: '/settings/recovery-kit' },
	];

	let query = '';
	let selectedIndex = 0;
	let inputEl: HTMLInputElement;
	let previousFocus: HTMLElement | null = null;

	$: filtered = query
		? routes.filter(r => r.label.toLowerCase().includes(query.toLowerCase()))
		: routes;

	$: if (filtered.length > 0 && selectedIndex >= filtered.length) {
		selectedIndex = filtered.length - 1;
	}

	function close() {
		dispatch('close');
		query = '';
		selectedIndex = 0;
		previousFocus?.focus();
		previousFocus = null;
	}

	function select(href: string) {
		goto(href);
		close();
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			close();
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			selectedIndex = Math.min(selectedIndex + 1, filtered.length - 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			selectedIndex = Math.max(selectedIndex - 1, 0);
		} else if (e.key === 'Enter') {
			if (filtered[selectedIndex]) {
				select(filtered[selectedIndex].href);
			}
		} else if (e.key === 'Tab') {
			e.preventDefault();
		}
	}

	function handleInputKeydown(e: KeyboardEvent) {
		handleKeydown(e);
	}

	async function onOpenChange(isOpen: boolean) {
		if (isOpen) {
			previousFocus = document.activeElement as HTMLElement;
			query = '';
			selectedIndex = 0;
			await tick();
			inputEl?.focus();
		}
	}

	$: onOpenChange(open);
</script>

{#if open}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-modal-backdrop bg-black/60 backdrop-blur-sm"
		on:click={close}
		on:keydown={(e) => e.key === 'Escape' && close()}
		role="button"
		tabindex="-1"
		aria-label="Close command palette"
	></div>

	<!-- Palette -->
	<div
		class="fixed top-[20%] left-1/2 -translate-x-1/2 z-modal w-full max-w-xl bg-bg-elevated border border-border rounded-xl shadow-xl animate-modal-in"
		role="dialog"
		aria-modal="true"
		aria-label="Command palette"
		tabindex="-1"
		on:keydown={handleKeydown}
	>
		<div class="border-b border-border px-4 py-3">
			<input
				bind:this={inputEl}
				type="text"
				bind:value={query}
				on:keydown={handleInputKeydown}
				placeholder="Search pages..."
				aria-label="Search pages"
				class="w-full bg-transparent text-text placeholder-text-tertiary text-sm outline-none"
			/>
		</div>
		<ul class="py-2 max-h-80 overflow-y-auto" role="listbox">
			{#each filtered as route, i}
				<li role="option" aria-selected={i === selectedIndex}>
					<button
						class="w-full text-left px-4 py-2.5 text-sm transition-colors
							{i === selectedIndex ? 'bg-primary/10 text-primary' : 'text-text-secondary hover:text-text hover:bg-bg-surface-hover'}"
						on:click={() => select(route.href)}
						on:mouseenter={() => (selectedIndex = i)}
					>
						{route.label}
					</button>
				</li>
			{/each}
			{#if filtered.length === 0}
				<li class="px-4 py-6 text-center text-sm text-text-tertiary">No results found.</li>
			{/if}
		</ul>
		<div class="border-t border-border px-4 py-2 flex items-center gap-4 text-xs text-text-tertiary">
			<span><kbd class="font-mono bg-bg-overlay px-1.5 py-0.5 rounded border border-border-muted">↑↓</kbd> navigate</span>
			<span><kbd class="font-mono bg-bg-overlay px-1.5 py-0.5 rounded border border-border-muted">↵</kbd> select</span>
			<span><kbd class="font-mono bg-bg-overlay px-1.5 py-0.5 rounded border border-border-muted">Esc</kbd> close</span>
		</div>
	</div>
{/if}
