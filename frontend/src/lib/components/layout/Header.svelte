<script lang="ts">
	import { page } from '$app/stores';
	import { sidebarOpen, backupRunning } from '$lib/stores/app';
	import { connected } from '$lib/stores/sse';

	export let title: string = 'Dashboard';

	const breadcrumbMap: Record<string, string> = {
		'': 'Dashboard',
		'backups': 'Backups',
		'snapshots': 'Snapshots',
		'databases': 'Databases',
		'activity': 'Activity',
		'logs': 'Logs',
		'restore': 'Restore',
		'settings': 'Settings',
		'general': 'General',
		'targets': 'Targets',
		'schedule': 'Schedule',
		'jobs': 'Jobs',
		'directories': 'Directories',
		'notifications': 'Notifications',
		'security': 'Security',
		'monitoring': 'Monitoring',
		'recovery-kit': 'Recovery Kit',
	};

	$: segments = $page.url.pathname.split('/').filter(Boolean);

	$: breadcrumbs = segments.map((seg, i) => ({
		label: breadcrumbMap[seg] ?? seg.charAt(0).toUpperCase() + seg.slice(1),
		href: '/' + segments.slice(0, i + 1).join('/'),
	}));
</script>

<header class="sticky top-0 z-30 bg-bg-app/80 backdrop-blur-sm border-b border-border-muted">
	<div class="flex items-center justify-between px-6 py-3">
		<div class="flex items-center gap-4">
			<button
				on:click={() => sidebarOpen.update(v => !v)}
				class="text-text-secondary hover:text-text lg:hidden"
				aria-label="Toggle sidebar"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>
			<div>
				<h2 class="text-lg font-semibold text-text leading-tight">{title}</h2>
				{#if breadcrumbs.length > 0}
					<nav class="flex items-center gap-1 text-xs text-text-tertiary mt-0.5" aria-label="Breadcrumb">
						<a href="/" class="hover:text-text-secondary transition-colors">Home</a>
						{#each breadcrumbs as crumb, i}
							<span class="mx-1 opacity-40">/</span>
							{#if i === breadcrumbs.length - 1}
								<span class="text-text-secondary">{crumb.label}</span>
							{:else}
								<a href={crumb.href} class="hover:text-text-secondary transition-colors">{crumb.label}</a>
							{/if}
						{/each}
					</nav>
				{/if}
			</div>
		</div>
		<div class="flex items-center gap-3">
			<span
				class="w-2 h-2 rounded-full shrink-0 {$connected ? 'bg-success' : 'bg-danger animate-pulse'}"
				title={$connected ? 'Live updates connected' : 'Live updates disconnected'}
			></span>
			{#if $backupRunning}
				<div class="flex items-center gap-2 text-sm text-primary">
					<div class="w-2 h-2 rounded-full bg-primary animate-pulse"></div>
					Backup Running
				</div>
			{/if}
		</div>
	</div>
</header>
