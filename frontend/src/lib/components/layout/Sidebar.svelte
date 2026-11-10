<script lang="ts">
	import { tick } from 'svelte';
	import { page } from '$lib/stores/page';
	import { sidebarOpen, systemStatus } from '$lib/stores/app';
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
		HeartPulse,
		KeyRound,
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
				{ href: '/settings/monitoring', label: 'Monitoring', icon: HeartPulse },
				{ href: '/settings/directories', label: 'Directories', icon: FolderOpen },
				{ href: '/settings/security', label: 'Security', icon: Shield },
				{ href: '/settings/recovery-kit', label: 'Recovery Kit', icon: KeyRound },
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

	// On narrow screens the open sidebar is an overlay that comes before the
	// page in DOM order: opening it moves focus to its first link, and Escape
	// closes it and hands focus back to the menu button that opened it.
	let navEl: HTMLElement | undefined;
	let wasOpen = $sidebarOpen;
	$: handleOpenChange($sidebarOpen);

	function handleOpenChange(open: boolean) {
		if (open && !wasOpen && isMobile()) {
			tick().then(() => navEl?.querySelector<HTMLElement>('a[href]')?.focus());
		}
		wasOpen = open;
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && $sidebarOpen && isMobile()) {
			sidebarOpen.set(false);
			document.getElementById('sidebar-toggle')?.focus();
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- Mobile backdrop -->
{#if $sidebarOpen}
	<button
		class="fixed inset-0 z-30 bg-black/50 lg:hidden"
		on:click={() => sidebarOpen.set(false)}
		aria-label="Close sidebar"
		tabindex="-1"
	></button>
{/if}

<!-- A closed sidebar is translated off-screen; `inert` on its nav and footer
     keeps those links out of the tab order and the accessibility tree instead
     of leaving invisible focus stops. The brand heading stays exposed: it is
     the page's h1 (page titles are h2, sections h3). -->
<aside
	id="app-sidebar"
	aria-label="Sidebar"
	class={cn(
		'fixed left-0 top-0 z-40 h-screen flex flex-col transition-transform duration-300',
		'w-64 bg-bg-base border-r border-border',
		$sidebarOpen ? 'translate-x-0' : '-translate-x-full'
	)}
>
	<!-- Logo -->
	<div class="flex items-center gap-3 px-5 py-5 border-b border-border shrink-0">
		<div class="w-8 h-8 rounded-lg bg-primary flex items-center justify-center" aria-hidden="true">
			<span class="text-white font-bold text-sm">A</span>
		</div>
		<div>
			<h1 class="text-base font-semibold text-text">Arkive</h1>
			<p class="text-[10px] text-text-tertiary tracking-wider uppercase">Disaster Recovery</p>
		</div>
	</div>

	<!-- Navigation -->
	<nav bind:this={navEl} class="flex-1 min-h-0 px-3 py-3 overflow-y-auto" aria-label="Main" inert={!$sidebarOpen}>
		{#each groups as group, gi}
			<div class={gi === 0 ? 'mb-1' : 'mt-4 mb-1'}>
				<span id="nav-group-{gi}" class="px-3 text-[11px] uppercase tracking-wider text-text-tertiary font-medium">
					{group.label}
				</span>
			</div>
			<ul class="space-y-0.5" aria-labelledby="nav-group-{gi}">
				{#each group.items as item}
					<li>
						<a
							href={item.href}
							aria-current={isActive(item.href) ? 'page' : undefined}
							class={cn(
								'nav-item flex items-center gap-3 px-3 py-2 rounded text-sm transition-colors',
								isActive(item.href)
									? 'active bg-primary/10 text-primary font-medium'
									: 'text-text-secondary hover:text-text hover:bg-bg-surface-hover'
							)}
						>
							<svelte:component this={item.icon} class="w-4 h-4 shrink-0" aria-hidden="true" />
							{item.label}
						</a>
					</li>
				{/each}
			</ul>
		{/each}
	</nav>

	<!-- Version: in normal flow so it never covers the last nav items on
	     short viewports. -->
	<div class="shrink-0 border-t border-border-muted px-5 py-3" inert={!$sidebarOpen}>
		<div class="text-[11px] text-text-tertiary flex items-center justify-between">
			<span>{$systemStatus?.version ? `v${$systemStatus.version}` : ''}</span>
			<span class="badge-success">Community</span>
		</div>
	</div>
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
