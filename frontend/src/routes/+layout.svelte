<script lang="ts">
	import '../app.css';
	import Sidebar from '$lib/components/layout/Sidebar.svelte';
	import Toasts from '$lib/components/layout/Toasts.svelte';
	import CommandPalette from '$lib/components/layout/CommandPalette.svelte';
	import { sidebarOpen, initTheme, setupCompleted, systemStatus, backupRunning } from '$lib/stores/app';
	import { applySession, authenticated } from '$lib/stores/auth';
	import * as sse from '$lib/stores/sse';
	import { api } from '$lib/api/client';
	import { loginUrl } from '$lib/api/session';
	import { isConflictTerminalEvent } from '$lib/utils/run-status';
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
					goto(loginUrl($page.url.pathname + $page.url.search));
				}
				return;
			}

			sse.connect();
			try {
				const status = await api.getStatus();
				systemStatus.set(status);
				setupCompleted.set(status.setup_completed);
			} catch (e) {
				console.error('Failed to load status during bootstrap:', e);
			}
		}).catch(e => {
			console.error('Failed to bootstrap session:', e);
		});

		// Global "backup running" tracking, independent of which page is open.
		// Terminal events for a different run (e.g. a conflict-skip of a
		// scheduled run while another backup holds the lock) must not clear it.
		let activeRunId: string | null = null;
		const onRunActive = (event: { data: Record<string, unknown> }) => {
			activeRunId = (event.data.run_id as string | undefined) ?? activeRunId;
			backupRunning.set(true);
		};
		const onRunEnded = (event: { data: Record<string, unknown> }) => {
			const runId = event.data.run_id as string | undefined;
			if (isConflictTerminalEvent(event.data)) return;
			if (activeRunId && runId && runId !== activeRunId) return;
			activeRunId = null;
			backupRunning.set(false);
		};
		const sseUnsubs = [
			sse.subscribe('backup:started', onRunActive),
			sse.subscribe('backup:progress', onRunActive),
			sse.subscribe('backup:completed', onRunEnded),
			sse.subscribe('backup:failed', onRunEnded),
			sse.subscribe('backup:cancelled', onRunEnded),
		];

		function handleKeydown(e: KeyboardEvent) {
			if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
				e.preventDefault();
				commandPaletteOpen = !commandPaletteOpen;
			}
		}

		window.addEventListener('keydown', handleKeydown);
		return () => {
			window.removeEventListener('keydown', handleKeydown);
			sseUnsubs.forEach((unsub) => unsub());
		};
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
