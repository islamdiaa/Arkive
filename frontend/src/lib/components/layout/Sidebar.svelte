<script lang="ts">
	import { page } from '$app/stores';
	import { sidebarOpen } from '$lib/stores/app';
	import { cn } from '$lib/utils/cn';
	import {
		LayoutDashboard,
		Activity,
		HardDrive,
		Camera,
		Database,
		RotateCcw,
		CalendarClock,
		FileText,
		Settings,
		Cloud,
		Clock,
		Bell,
		FolderOpen,
		Shield,
	} from 'lucide-svelte';

	const groups = [
		{
			label: 'OVERVIEW',
			items: [
				{ href: '/', label: 'Dashboard', icon: LayoutDashboard },
				{ href: '/activity', label: 'Activity', icon: Activity },
			],
		},
		{
			label: 'DATA',
			items: [
				{ href: '/backups', label: 'Backups', icon: HardDrive },
				{ href: '/snapshots', label: 'Snapshots', icon: Camera },
				{ href: '/databases', label: 'Databases', icon: Database },
				{ href: '/restore', label: 'Restore', icon: RotateCcw },
			],
		},
		{
			label: 'MONITOR',
			items: [
				{ href: '/settings/jobs', label: 'Jobs', icon: CalendarClock },
				{ href: '/logs', label: 'Logs', icon: FileText },
			],
		},
		{
			label: 'CONFIG',
			items: [
				{ href: '/settings/general', label: 'Settings', icon: Settings },
				{ href: '/settings/targets', label: 'Targets', icon: Cloud },
				{ href: '/settings/schedule', label: 'Schedule', icon: Clock },
				{ href: '/settings/notifications', label: 'Notifications', icon: Bell },
				{ href: '/settings/directories', label: 'Directories', icon: FolderOpen },
				{ href: '/settings/security', label: 'Security', icon: Shield },
			],
		},
	];

	$: currentPath = $page.url.pathname;

	function isActive(href: string): boolean {
		if (href === '/') return currentPath === '/';
		return currentPath === href || currentPath.startsWith(href + '/');
	}

	// Close sidebar on mobile when navigating
	$: if (currentPath && isMobile()) {
		sidebarOpen.set(false);
	}

	function isMobile(): boolean {
		return typeof window !== 'undefined' && window.innerWidth < 1024;
	}
</script>

<!-- Mobile backdrop -->
{#if $sidebarOpen}
	<button
		class="fixed inset-0 z-30 bg-black/50 lg:hidden"
		on:click={() => sidebarOpen.set(false)}
		aria-label="Close sidebar"
		tabindex="-1"
	></button>
{/if}

<aside class={cn(
	'fixed left-0 top-0 z-40 h-screen transition-transform duration-300',
	'w-64 bg-bg-base border-r border-border',
	$sidebarOpen ? 'translate-x-0' : '-translate-x-full'
)}>
	<!-- Logo -->
	<div class="flex items-center gap-3 px-5 py-5 border-b border-border">
		<div class="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
			<span class="text-white font-bold text-sm">A</span>
		</div>
		<div>
			<h1 class="text-base font-semibold text-text">Arkive</h1>
			<p class="text-[10px] text-text-tertiary tracking-wider uppercase">Disaster Recovery</p>
		</div>
	</div>

	<!-- Navigation -->
	<nav class="px-3 py-3 overflow-y-auto h-[calc(100vh-80px)]">
		{#each groups as group, gi}
			<div class={gi === 0 ? 'mb-1' : 'mt-4 mb-1'}>
				<span class="px-3 text-[11px] uppercase tracking-wider text-text-tertiary font-medium">
					{group.label}
				</span>
			</div>
			<ul class="space-y-0.5">
				{#each group.items as item}
					<li>
						<a
							href={item.href}
							class={cn(
								'nav-item flex items-center gap-3 px-3 py-2 rounded text-sm transition-colors',
								isActive(item.href)
									? 'active bg-primary/10 text-primary font-medium'
									: 'text-text-secondary hover:text-text hover:bg-bg-surface-hover'
							)}
						>
							<svelte:component this={item.icon} class="w-4 h-4 shrink-0" />
							{item.label}
						</a>
					</li>
				{/each}
			</ul>
		{/each}

		<!-- Version -->
		<div class="absolute bottom-4 left-0 right-0 px-5">
			<div class="text-[11px] text-text-tertiary flex items-center justify-between">
				<span>v0.1.0</span>
				<span class="badge-success">Community</span>
			</div>
		</div>
	</nav>
</aside>

<style>
	.nav-item {
		position: relative;
	}
	.nav-item::before {
		content: '';
		position: absolute;
		left: 0;
		top: 20%;
		bottom: 20%;
		width: 2px;
		background: var(--color-primary);
		transform: scaleY(0);
		transition: transform 150ms ease-out;
		border-radius: 0 1px 1px 0;
	}
	.nav-item.active::before {
		transform: scaleY(1);
	}
</style>
