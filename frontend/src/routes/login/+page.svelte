<svelte:head>
	<title>Login | Arkive</title>
</svelte:head>

<script lang="ts">
	import { api } from '$lib/api/client';
	import { safeNext } from '$lib/api/session';
	import { addToast, setupCompleted } from '$lib/stores/app';
	import { applySession } from '$lib/stores/auth';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	let apiKey = '';
	let loading = false;

	function nextDestination(): string {
		if (typeof window === 'undefined') return '/';
		return safeNext(new URLSearchParams(window.location.search).get('next'));
	}

	onMount(async () => {
		try {
			const session = await api.getSession();
			if (session.setup_required) {
				goto('/setup');
				return;
			}
			if (session.authenticated) {
				goto(nextDestination());
			}
		} catch {
			// Ignore and let the user attempt login.
		}
	});

	async function login() {
		if (!apiKey.trim()) {
			addToast({ type: 'error', message: 'API key is required' });
			return;
		}

		loading = true;
		try {
			const session = await api.login(apiKey.trim());
			applySession(session);
			setupCompleted.set(true);
			goto(nextDestination());
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Login failed' });
		} finally {
			loading = false;
		}
	}
</script>

<div class="min-h-screen bg-page flex items-center justify-center p-6">
	<div class="w-full max-w-md card p-8 space-y-6">
		<div class="space-y-2 text-center">
			<h1 class="text-2xl font-bold text-text">Sign in to Arkive</h1>
			<p class="text-sm text-text-secondary">
				Use your admin API key once to open a browser session. The key stays out of `localStorage`.
			</p>
		</div>

		<div class="space-y-2">
			<label class="text-sm font-medium text-text" for="api-key">Admin API key</label>
			<input
				id="api-key"
				bind:value={apiKey}
				type="password"
				autocomplete="current-password"
				class="input w-full"
				placeholder="ark_..."
				on:keydown={(event) => event.key === 'Enter' && login()}
			/>
		</div>

		<button class="btn-primary w-full disabled:opacity-50" disabled={loading} on:click={login}>
			{loading ? 'Signing In...' : 'Sign In'}
		</button>
	</div>
</div>
