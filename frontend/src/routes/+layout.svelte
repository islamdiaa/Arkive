<script lang="ts">
	import '../app.css';
	import Sidebar from '$lib/components/layout/Sidebar.svelte';
	import Toasts from '$lib/components/layout/Toasts.svelte';
	import CommandPalette from '$lib/components/layout/CommandPalette.svelte';
	import { sidebarOpen, initTheme, setupCompleted, systemStatus } from '$lib/stores/app';
	import { applySession, authenticated } from '$lib/stores/auth';
	import * as sse from '$lib/stores/sse';
	import { api } from '$lib/api/client';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { fly, fade } from 'svelte/transition';
	import { onMount } from 'svelte';
	import { cn } from '$lib/utils/cn';
	import ConnectionBanner from '$lib/components/shared/ConnectionBanner.svelte';

	let commandPaletteOpen = false;

	onMount(() => {
		initTheme();
		// Default sidebar closed on mobile
		if (window.innerWidth < 1024) {
			sidebarOpen.set(false);
		}
		api.getSession().then(async (session) => {
			applySession(session);
			if (session.setup_required) {
				setupCompleted.set(false);
				if ($page.url.pathname !== '/setup') {
					goto('/setup');
				}
				return;
			}

			setupCompleted.set(true);
			if (!session.authenticated) {
				sse.disconnect();
				if ($page.url.pathname !== '/login') {
					goto('/login');
				}
				return;
			}

			const status = await api.getStatus();
			systemStatus.set(status);
			setupCompleted.set(status.setup_completed);
			sse.connect();
		}).catch(e => {
			console.error('Failed to bootstrap session:', e);
		});

		function handleKeydown(e: KeyboardEvent) {
			if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
				e.preventDefault();
				commandPaletteOpen = !commandPaletteOpen;
			}
		}

		window.addEventListener('keydown', handleKeydown);
		return () => window.removeEventListener('keydown', handleKeydown);
	});

	$: isSetupPage = $page.url.pathname === '/setup';
	$: isLoginPage = $page.url.pathname === '/login';

	// Reactively connect SSE when setup completes (covers post-setup navigation
	// where onMount doesn't re-run)
	$: if ($setupCompleted && $authenticated) {
		sse.connect();
	}

	$: if (!$authenticated) {
		sse.disconnect();
	}
</script>

<CommandPalette open={commandPaletteOpen} on:close={() => (commandPaletteOpen = false)} />

{#if isSetupPage || isLoginPage}
	<slot />
{:else}
	<div class="min-h-screen bg-page">
		<Sidebar />
		<div class={cn('transition-all duration-300', $sidebarOpen ? 'lg:ml-64' : '')}>
			<ConnectionBanner />
			{#key $page.url.pathname}
				<div
					in:fly={{ y: 6, duration: 180, delay: 60 }}
					out:fade={{ duration: 80 }}
				>
					<slot />
				</div>
			{/key}
		</div>
	</div>
{/if}

<Toasts />
