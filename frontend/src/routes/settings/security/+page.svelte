<svelte:head>
	<title>Security | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { onMount } from 'svelte';
	import { Shield, Key, Lock } from 'lucide-svelte';
	import SectionCard from '$lib/components/ui/SectionCard.svelte';
	import ConfirmDangerModal from '$lib/components/ui/ConfirmDangerModal.svelte';

	let settings: any = null;
	let loading = true;
	let error = '';
	let rotateModalOpen = false;
	let rotating = false;
	let rotatedKey = '';

	onMount(async () => {
		try {
			settings = await api.getSettings();
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load security settings';
		}
		loading = false;
	});

	async function doRotateKey() {
		rotating = true;
		try {
			const result = await api.rotateKey();
			rotatedKey = result.api_key;
			rotateModalOpen = false;
			addToast({ type: 'success', message: 'API key rotated. Save the new key for CLI or integrations.' });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		rotating = false;
	}

	const securityFeatures = [
		'AES-256-CTR encryption at rest',
		'API key authentication (SHA-256 hashed)',
		'Sensitive values encrypted in database',
		'Docker socket read-only access',
	];
</script>

<Header title="Security" />

<ConfirmDangerModal
	bind:open={rotateModalOpen}
	title="Rotate API Key"
	message="The current API key will be permanently invalidated. You will need to update any integrations using the current key."
	confirmText="ROTATE"
	confirmLabel="Rotate Key"
	loading={rotating}
	on:confirm={doRotateKey}
	on:cancel={() => rotateModalOpen = false}
/>

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	{#if loading}
		<div class="card animate-skeleton h-48"></div>
	{:else}
		<SectionCard title="API Key" description="Your API key is now for CLI and integrations only. The web app uses a browser session cookie.">
			<div class="flex items-center justify-between">
				<div class="flex items-center gap-3">
					<div class="w-9 h-9 rounded-lg bg-bg-surface border border-border flex items-center justify-center">
						<Key size={16} class="text-text-secondary" />
					</div>
					<div>
						<p class="text-sm font-mono text-text">{settings?.api_key_set ? 'Configured' : 'Not set'}</p>
						<p class="text-xs text-text-secondary mt-0.5">Integration credential</p>
					</div>
				</div>
				<button on:click={() => rotateModalOpen = true} class="btn-danger text-sm">Rotate API Key</button>
			</div>
			{#if rotatedKey}
				<div class="mt-4 p-3 bg-warning/10 border border-warning/30 rounded">
					<p class="text-xs font-medium text-warning mb-1">New API key</p>
					<p class="text-sm font-mono break-all text-text">{rotatedKey}</p>
				</div>
			{/if}
		</SectionCard>

		<SectionCard title="Encryption" description="All backup data is encrypted using your encryption password via restic's built-in encryption (AES-256-CTR + Poly1305-AES).">
			<div class="flex items-center gap-3">
				<div class="w-9 h-9 rounded-lg bg-bg-surface border border-border flex items-center justify-center">
					<Lock size={16} class="text-text-secondary" />
				</div>
				<div>
					<span class="badge badge-success">Encryption Password Set</span>
					<p class="text-xs text-text-secondary mt-1">Password cannot be changed after setup. If lost, backups cannot be restored.</p>
				</div>
			</div>
			<div class="p-3 bg-warning/10 border border-warning/30 rounded text-sm text-warning">
				Your encryption password cannot be changed after setup. If lost, backups cannot be restored.
			</div>
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<p class="text-sm font-medium text-text">Recovery kit</p>
					<p class="text-xs text-text-secondary mt-0.5">
						{settings?.recovery_kit_confirmed_at
							? `Saved off-server — confirmed ${new Date(settings.recovery_kit_confirmed_at).toLocaleDateString()}`
							: 'Store the encryption password and repository locations somewhere other than this server.'}
					</p>
				</div>
				<a href="/settings/recovery-kit" class="btn-secondary text-sm">Open recovery kit</a>
			</div>
		</SectionCard>

		<SectionCard title="Security Features" description="Active protections in this Arkive instance">
			<div class="space-y-3">
				{#each securityFeatures as feature}
					<div class="flex items-center gap-3">
						<div class="w-5 h-5 rounded-full bg-success/15 flex items-center justify-center flex-shrink-0">
							<svg class="w-3 h-3 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" /></svg>
						</div>
						<span class="text-sm text-text">{feature}</span>
					</div>
				{/each}
			</div>
		</SectionCard>

		<SectionCard title="Danger Zone" description="Destructive actions that cannot be undone" variant="danger">
			<div class="flex items-center justify-between">
				<div>
					<p class="text-sm font-medium text-text">Rotate API Key</p>
					<p class="text-xs text-text-secondary mt-0.5">Invalidates the current API key immediately</p>
				</div>
				<button on:click={() => rotateModalOpen = true} class="btn-danger text-sm">Rotate Key</button>
			</div>
		</SectionCard>
	{/if}
</main>
