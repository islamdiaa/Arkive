<svelte:head>
	<title>Storage Targets | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { formatBytes } from '$lib/utils/format';
	import { timeAgo } from '$lib/utils/date';
	import { onMount } from 'svelte';
	import { Cloud, Folder, Server, HardDrive } from 'lucide-svelte';
	import ConfirmDangerModal from '$lib/components/ui/ConfirmDangerModal.svelte';
	import { isHealthyTargetStatus } from '$lib/utils/run-status';

	let targets: any[] = [];
	let loading = true;
	let error = '';
	let showAdd = false;
	let testing = '';
	let editingId = '';
	let editName = '';
	let deleteConfirmId = '';
	let deleteConfirmOpen = false;
	let deleteConfirmName = '';

	// Add form
	let newName = '';
	let newType = 'b2';
	let newConfig: Record<string, string> = {};

	const targetTypes = [
		{ value: 'b2', label: 'Backblaze B2' },
		{ value: 's3', label: 'Amazon S3' },
		{ value: 'wasabi', label: 'Wasabi' },
		{ value: 'sftp', label: 'SFTP' },
		{ value: 'dropbox', label: 'Dropbox' },
		{ value: 'gdrive', label: 'Google Drive' },
		{ value: 'local', label: 'Local Path' },
	];

	const configFields: Record<string, { key: string; label: string; type: string }[]> = {
		b2: [
			{ key: 'key_id', label: 'Application Key ID', type: 'text' },
			{ key: 'app_key', label: 'Application Key', type: 'password' },
			{ key: 'bucket', label: 'Bucket Name', type: 'text' },
		],
		s3: [
			{ key: 'endpoint', label: 'Endpoint URL', type: 'text' },
			{ key: 'access_key', label: 'Access Key', type: 'text' },
			{ key: 'secret_key', label: 'Secret Key', type: 'password' },
			{ key: 'bucket', label: 'Bucket Name', type: 'text' },
			{ key: 'region', label: 'Region', type: 'text' },
		],
		sftp: [
			{ key: 'host', label: 'Host', type: 'text' },
			{ key: 'username', label: 'Username', type: 'text' },
			{ key: 'password', label: 'Password', type: 'password' },
			{ key: 'remote_path', label: 'Remote Path', type: 'text' },
		],
		local: [
			{ key: 'path', label: 'Local Path', type: 'text' },
		],
		wasabi: [
			{ key: 'access_key', label: 'Access Key', type: 'text' },
			{ key: 'secret_key', label: 'Secret Key', type: 'password' },
			{ key: 'bucket', label: 'Bucket Name', type: 'text' },
			{ key: 'region', label: 'Region', type: 'text' },
		],
		dropbox: [
			{ key: 'token', label: 'Access Token', type: 'password' },
		],
		gdrive: [
			{ key: 'client_id', label: 'Client ID', type: 'text' },
			{ key: 'client_secret', label: 'Client Secret', type: 'password' },
			{ key: 'token', label: 'OAuth Token', type: 'password' },
		],
	};

	function getProviderIcon(type: string) {
		if (type === 'local') return Folder;
		if (type === 'sftp') return Server;
		return Cloud;
	}

	onMount(() => {
		fetchTargets();
	});

	async function fetchTargets() {
		loading = true;
		error = '';
		try {
			const result = await api.listTargets();
			targets = result.items || result.targets || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load storage targets';
		}
		loading = false;
	}

	async function addTarget() {
		try {
			await api.createTarget({ name: newName, type: newType, config: newConfig });
			addToast({ type: 'success', message: `Target "${newName}" added` });
			showAdd = false;
			newName = '';
			newConfig = {};
			await fetchTargets();
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	async function testTarget(id: string) {
		testing = id;
		try {
			const result = await api.testTarget(id);
			// Backend returns { success, message, tested_at }.
			const ok = result?.success === true;
			addToast({
				type: ok ? 'success' : 'error',
				message: result?.message || (ok ? 'Connection test passed' : 'Connection test failed'),
			});
			await fetchTargets();
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		testing = '';
	}

	function startEdit(target: any) {
		editingId = target.id;
		editName = target.name;
	}

	async function saveEdit() {
		if (!editName.trim()) return;
		try {
			await api.updateTarget(editingId, { name: editName });
			addToast({ type: 'success', message: 'Target updated' });
			editingId = '';
			editName = '';
			await fetchTargets();
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	function cancelEdit() {
		editingId = '';
		editName = '';
	}

	function openDeleteConfirm(target: any) {
		deleteConfirmId = target.id;
		deleteConfirmName = target.name;
		deleteConfirmOpen = true;
	}

	async function confirmDeleteTarget() {
		try {
			await api.deleteTarget(deleteConfirmId);
			addToast({ type: 'success', message: 'Target deleted' });
			deleteConfirmId = '';
			deleteConfirmOpen = false;
			await fetchTargets();
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}
</script>

<Header title="Storage Targets" />

<ConfirmDangerModal
	bind:open={deleteConfirmOpen}
	title="Delete Storage Target"
	message="Are you sure you want to delete &quot;{deleteConfirmName}&quot;? This cannot be undone."
	confirmText="DELETE"
	confirmLabel="Delete Target"
	on:confirm={confirmDeleteTarget}
	on:cancel={() => { deleteConfirmOpen = false; deleteConfirmId = ''; }}
/>

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	<div class="flex items-center justify-between">
		<p class="text-sm text-text-secondary">{targets.length} storage target{targets.length !== 1 ? 's' : ''} configured</p>
		<button on:click={() => showAdd = !showAdd} class="btn-primary text-sm">
			{showAdd ? 'Cancel' : 'Add Target'}
		</button>
	</div>

	{#if showAdd}
		<div class="card">
			<h3 class="font-semibold text-text mb-4">Add Storage Target</h3>
			<div class="space-y-4">
				<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
					<div>
						<label for="target-new-name" class="block text-sm text-text-secondary mb-1">Name</label>
						<input id="target-new-name" type="text" bind:value={newName} class="input" placeholder="My B2 Bucket" />
					</div>
					<div>
						<label for="target-new-type" class="block text-sm text-text-secondary mb-1">Type</label>
						<select id="target-new-type" bind:value={newType} class="input" on:change={() => newConfig = {}}>
							{#each targetTypes as t}
								<option value={t.value}>{t.label}</option>
							{/each}
						</select>
					</div>
				</div>
				{#if configFields[newType]}
					<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
						{#each configFields[newType] as field}
							<div>
								<label for="target-cfg-{field.key}" class="block text-sm text-text-secondary mb-1">{field.label}</label>
								<input id="target-cfg-{field.key}" type={field.type} bind:value={newConfig[field.key]} class="input" />
							</div>
						{/each}
					</div>
				{/if}
				<button on:click={addTarget} disabled={!newName} class="btn-primary disabled:opacity-50">Save Target</button>
			</div>
		</div>
	{/if}

	{#if loading}
		<div class="space-y-4">
			{#each Array(2) as _}
				<div class="card animate-skeleton h-24"></div>
			{/each}
		</div>
	{:else if targets.length === 0}
		<div class="card text-center py-12">
			<svg class="w-12 h-12 text-text-secondary mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z" /></svg>
			<p class="text-text-secondary">No storage targets configured yet.</p>
			<button on:click={() => showAdd = true} class="btn-primary mt-4">Add Your First Target</button>
		</div>
	{:else}
		<div class="space-y-4">
			{#each targets as target}
				{@const ProviderIcon = getProviderIcon(target.type)}
				<div class="card">
					<div class="flex items-center justify-between">
						<div class="flex items-center gap-4">
							<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
								<svelte:component this={ProviderIcon} size={18} class="text-primary" />
							</div>
							<div>
								{#if editingId === target.id}
									<div class="flex items-center gap-2">
										<input type="text" bind:value={editName} class="input text-sm py-1 px-2 w-48" on:keydown={(e) => e.key === 'Enter' && saveEdit()} />
										<button on:click={saveEdit} class="btn-primary text-xs py-1 px-2">Save</button>
										<button on:click={cancelEdit} class="btn-secondary text-xs py-1 px-2">Cancel</button>
									</div>
								{:else}
									<div class="flex items-center gap-2">
										<p class="text-sm font-medium text-text">{target.name}</p>
										<span class="badge {isHealthyTargetStatus(target.status) ? 'badge-success' : target.status === 'error' ? 'badge-danger' : 'badge-warning'}">{isHealthyTargetStatus(target.status) ? 'healthy' : target.status || 'unknown'}</span>
									</div>
								{/if}
								<p class="text-xs font-mono text-text-secondary mt-0.5">
									{target.type} &middot; {target.snapshot_count || 0} snapshots &middot; {formatBytes(target.total_size_bytes || 0)}
									{#if target.last_tested}
										&middot; tested {timeAgo(target.last_tested)}
									{/if}
								</p>
							</div>
						</div>
						<div class="flex items-center gap-2">
							<button on:click={() => testTarget(target.id)} disabled={testing === target.id} class="btn-secondary text-xs disabled:opacity-50">
								{testing === target.id ? 'Testing...' : 'Test'}
							</button>
							<button on:click={() => startEdit(target)} class="text-text-secondary hover:text-primary" aria-label="Edit target">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" /></svg>
							</button>
							<button on:click={() => openDeleteConfirm(target)} class="text-text-secondary hover:text-danger" aria-label="Delete target">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
							</button>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</main>
