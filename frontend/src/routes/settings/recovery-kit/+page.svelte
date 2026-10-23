<svelte:head>
	<title>Recovery Kit | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import SectionCard from '$lib/components/ui/SectionCard.svelte';
	import { api } from '$lib/api/client';
	import { confirmRecoveryKit, generateRecoveryKit } from '$lib/api/settings';
	import { addToast, systemStatus } from '$lib/stores/app';
	import { copyText, downloadTextFile } from '$lib/utils/download';
	import { formatDate } from '$lib/utils/date';
	import type { RecoveryKit } from '$lib/types';
	import { beforeNavigate } from '$app/navigation';
	import { onDestroy, onMount } from 'svelte';
	import { Copy, Download, Eye, EyeOff, KeyRound, ShieldAlert } from 'lucide-svelte';

	const KIT_FILENAME = 'arkive-recovery-kit.txt';
	const MASK = '••••••••••••••••••••';

	// The kit holds the encryption password. It lives only in this component's
	// local state: never in a store, localStorage or the URL, and it is wiped
	// when the user navigates away.
	let kit: RecoveryKit | null = null;
	let revealPassword = false;
	let revealKeyfile = false;
	let storedElsewhere = false;

	let confirmedAt: string | null = null;
	let loading = true;
	let loadError = '';
	let generating = false;
	let generateError = '';
	let confirming = false;

	function clearKit() {
		kit = null;
		revealPassword = false;
		revealKeyfile = false;
		storedElsewhere = false;
	}

	beforeNavigate(clearKit);
	onDestroy(clearKit);

	onMount(async () => {
		try {
			const settings = await api.getSettings();
			confirmedAt = settings?.recovery_kit_confirmed_at ?? null;
		} catch (e: any) {
			loadError = e?.message || 'Failed to load recovery kit status';
		}
		loading = false;
	});

	async function generate() {
		generating = true;
		generateError = '';
		clearKit();
		try {
			kit = await generateRecoveryKit();
		} catch (e: any) {
			generateError = e?.message || 'Failed to generate recovery kit';
		}
		generating = false;
	}

	function download() {
		if (!kit) return;
		downloadTextFile(kit.text, KIT_FILENAME);
		addToast({ type: 'info', message: `Saved ${KIT_FILENAME}. Move it off this server, then delete local copies.` });
	}

	async function copyKit() {
		if (!kit) return;
		const ok = await copyText(kit.text);
		addToast(ok
			? { type: 'success', message: 'Recovery kit copied. Paste it into your password manager.' }
			: { type: 'error', message: 'Copy failed — use Download instead.' });
	}

	async function confirm() {
		if (!storedElsewhere || !kit) return;
		confirming = true;
		try {
			const result = await confirmRecoveryKit();
			confirmedAt = result?.confirmed_at ?? new Date().toISOString();
			systemStatus.update((s) => (s ? { ...s, recovery_kit_confirmed: true } : s));
			addToast({ type: 'success', message: 'Recovery kit marked as saved' });
			clearKit();
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Failed to confirm recovery kit' });
		}
		confirming = false;
	}
</script>

<Header title="Recovery Kit" />

<div class="p-6 space-y-6">
	<div>
		<h1 class="text-2xl font-semibold text-text">Recovery Kit</h1>
		<p class="text-sm text-text-secondary mt-1">Everything you need to restore your backups if this server is gone</p>
	</div>

	{#if loadError}
		<div class="p-4 bg-danger-bg border border-danger-muted rounded text-danger text-sm">{loadError}</div>
	{/if}

	{#if loading}
		<div class="card animate-skeleton h-48"></div>
	{:else}
		<SectionCard
			title="What is a recovery kit?"
			description="Your backups are encrypted with a password that is stored on this server. If this server dies, the backups on your storage targets are unreadable without that password."
		>
			<ul class="space-y-1.5 text-sm text-text-secondary list-disc pl-5">
				<li>The <span class="text-text font-medium">encryption password</span> for your backup repositories</li>
				<li>Where each <span class="text-text font-medium">repository</span> lives (bucket, path or host)</li>
				<li><span class="text-text font-medium">Step-by-step instructions</span> to restore with restic on any machine, without Arkive</li>
			</ul>
			<p class="text-sm text-text-secondary">
				Generate it, store it somewhere other than this server (a password manager or a printed copy), then confirm below.
			</p>

			{#if confirmedAt}
				<div class="flex items-center gap-2 rounded-md border border-success-muted bg-success-bg px-3 py-2 text-sm text-success" data-testid="recovery-kit-confirmed">
					<KeyRound class="w-4 h-4 shrink-0" />
					Recovery kit saved — confirmed {formatDate(confirmedAt)}
				</div>
			{:else}
				<div class="flex items-center gap-2 rounded-md border border-warning-muted bg-warning-bg px-3 py-2 text-sm text-warning">
					<KeyRound class="w-4 h-4 shrink-0" />
					You haven't confirmed saving a recovery kit yet.
				</div>
			{/if}

			<div class="flex flex-wrap items-center gap-3">
				<button on:click={generate} disabled={generating} class="btn-primary text-sm disabled:opacity-50">
					{#if generating}Generating...{:else if kit}Generate again{:else}Generate recovery kit{/if}
				</button>
				<span class="text-xs text-text-muted">Generating is logged in the activity feed.</span>
			</div>

			{#if generateError}
				<div class="p-3 bg-danger-bg border border-danger-muted rounded text-danger text-sm">{generateError}</div>
			{/if}
		</SectionCard>

		{#if kit}
			<SectionCard title="Your recovery kit" description="Generated {formatDate(kit.generated_at)} for {kit.server_name || 'this server'} · Arkive {kit.arkive_version}">
				<div class="flex items-start gap-3 rounded-md border border-danger bg-danger-bg px-3 py-2.5" role="alert">
					<ShieldAlert class="w-5 h-5 text-danger shrink-0 mt-0.5" />
					<div class="text-sm">
						<p class="font-medium text-danger">This kit contains secrets</p>
						<p class="text-text-secondary mt-0.5">
							Anyone who has it can decrypt and read all of your backups. Keep it in a password manager or on paper. Don't store it on this server, and don't email it to yourself.
						</p>
					</div>
				</div>

				<div class="space-y-1">
					<p class="block text-xs font-semibold uppercase tracking-wide text-text-secondary">Encryption password</p>
					<div class="flex items-center gap-2">
						<code class="flex-1 min-w-0 rounded-md border border-border bg-bg-input px-3 py-2 text-sm font-mono text-text break-all {revealPassword ? 'select-all' : 'select-none'}" data-testid="recovery-kit-password">
							{revealPassword ? kit.encryption_password : MASK}
						</code>
						<button
							type="button"
							class="btn-ghost btn-sm flex items-center gap-1.5"
							on:click={() => (revealPassword = !revealPassword)}
							aria-pressed={revealPassword}
						>
							{#if revealPassword}<EyeOff class="w-3.5 h-3.5" /> Hide{:else}<Eye class="w-3.5 h-3.5" /> Reveal{/if}
						</button>
					</div>
				</div>

				{#if kit.config_keyfile}
					<div class="space-y-1">
						<p class="block text-xs font-semibold uppercase tracking-wide text-text-secondary">Config keyfile</p>
						<div class="flex items-center gap-2">
							<code class="flex-1 min-w-0 rounded-md border border-border bg-bg-input px-3 py-2 text-sm font-mono text-text break-all {revealKeyfile ? 'select-all' : 'select-none'}">
								{revealKeyfile ? kit.config_keyfile : MASK}
							</code>
							<button
								type="button"
								class="btn-ghost btn-sm flex items-center gap-1.5"
								on:click={() => (revealKeyfile = !revealKeyfile)}
								aria-pressed={revealKeyfile}
							>
								{#if revealKeyfile}<EyeOff class="w-3.5 h-3.5" /> Hide{:else}<Eye class="w-3.5 h-3.5" /> Reveal{/if}
							</button>
						</div>
						<p class="text-xs text-text-muted">Decrypts the storage target credentials saved in Arkive's own database.</p>
					</div>
				{/if}

				<div class="space-y-2">
					<p class="block text-xs font-semibold uppercase tracking-wide text-text-secondary">Repositories</p>
					{#if kit.targets.length === 0}
						<p class="text-sm text-text-secondary">No storage targets are configured yet.</p>
					{:else}
						<div class="overflow-x-auto rounded-md border border-border">
							<table class="w-full text-sm">
								<thead>
									<tr class="text-left text-xs uppercase tracking-wide text-text-muted bg-bg-elevated">
										<th class="px-3 py-2 font-medium">Name</th>
										<th class="px-3 py-2 font-medium">Type</th>
										<th class="px-3 py-2 font-medium">Repository</th>
										<th class="px-3 py-2 font-medium">Host path</th>
									</tr>
								</thead>
								<tbody>
									{#each kit.targets as target (target.id)}
										<tr class="border-t border-border-muted align-top">
											<td class="px-3 py-2 text-text">{target.name}</td>
											<td class="px-3 py-2 text-text-secondary">{target.type}</td>
											<td class="px-3 py-2 font-mono text-xs text-text break-all">{target.repository}</td>
											<td class="px-3 py-2 font-mono text-xs text-text-secondary break-all">{target.host_path || '—'}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
				</div>

				{#if kit.instructions.length}
					<div class="space-y-2">
						<p class="block text-xs font-semibold uppercase tracking-wide text-text-secondary">How to restore</p>
						<ol class="list-decimal pl-5 space-y-1 text-sm text-text-secondary">
							{#each kit.instructions as step}
								<li class="break-words">{step}</li>
							{/each}
						</ol>
					</div>
				{/if}

				<div class="flex flex-wrap items-center gap-2">
					<button type="button" on:click={download} class="btn-secondary text-sm flex items-center gap-1.5">
						<Download class="w-4 h-4" /> Download (.txt)
					</button>
					<button type="button" on:click={copyKit} class="btn-secondary text-sm flex items-center gap-1.5">
						<Copy class="w-4 h-4" /> Copy
					</button>
				</div>

				<div class="border-t border-border-muted pt-4 space-y-3">
					<label class="flex items-start gap-2 text-sm text-text cursor-pointer">
						<input type="checkbox" bind:checked={storedElsewhere} required class="mt-0.5 rounded" />
						<span>I stored this somewhere other than this server (password manager, printed copy)</span>
					</label>
					<button
						type="button"
						on:click={confirm}
						disabled={!storedElsewhere || confirming}
						class="btn-primary text-sm disabled:opacity-50 disabled:cursor-not-allowed"
					>
						{confirming ? 'Confirming...' : 'Confirm'}
					</button>
				</div>
			</SectionCard>
		{/if}
	{/if}
</div>
