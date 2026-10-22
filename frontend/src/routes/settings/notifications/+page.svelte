<svelte:head>
	<title>Notifications | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { onMount } from 'svelte';
	import { MessageSquare, Hash, Bell, Mail, Webhook } from 'lucide-svelte';
	import FormToggle from '$lib/components/ui/FormToggle.svelte';
	import ConfirmDangerModal from '$lib/components/ui/ConfirmDangerModal.svelte';

	let channels: any[] = [];
	let loading = true;
	let error = '';
	let showAdd = false;
	let testing = '';
	let editingId = '';
	let editName = '';
	let deleteConfirmId = '';
	let deleteConfirmOpen = false;
	let deleteConfirmName = '';

	let newName = '';
	let newType = 'discord';
	let newUrl = '';
	let newEvents: string[] = ['backup.success', 'backup.failed'];

	// Must match NotificationChannelCreate.type in backend/app/models/notifications.py
	const channelTypes = ['discord', 'slack', 'telegram', 'email', 'ntfy', 'gotify', 'pushover', 'webhook', 'uptimekuma'];
	const eventOptions = ['backup.success', 'backup.failed', 'backup.started', 'restore.completed', 'system.error', 'system.warning'];

	function getChannelIcon(type: string) {
		if (type === 'discord') return MessageSquare;
		if (type === 'slack') return Hash;
		if (type === 'ntfy') return Bell;
		if (type === 'email') return Mail;
		return Webhook;
	}

	onMount(async () => {
		try {
			const result = await api.listChannels();
			channels = result.items || result.channels || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load notification channels';
		}
		loading = false;
	});

	async function addChannel() {
		try {
			await api.createChannel({ name: newName, type: newType, url: newUrl, events: newEvents, enabled: true });
			addToast({ type: 'success', message: `Channel "${newName}" added` });
			showAdd = false;
			newName = '';
			newUrl = '';
			const result = await api.listChannels();
			channels = result.items || result.channels || [];
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	async function testChannel(id: string) {
		testing = id;
		try {
			// Backend returns HTTP 200 with { status: 'ok' | 'failed', error? }.
			const result = await api.testChannel(id);
			if (result?.status === 'ok') {
				addToast({ type: 'success', message: 'Test notification sent' });
			} else {
				addToast({
					type: 'error',
					message: `Test notification failed${result?.error ? `: ${result.error}` : ' — check the channel URL'}`,
				});
			}
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		testing = '';
	}

	function startEditChannel(ch: any) {
		editingId = ch.id;
		editName = ch.name;
	}

	async function saveEditChannel() {
		if (!editName.trim()) return;
		try {
			await api.updateChannel(editingId, { name: editName });
			addToast({ type: 'success', message: 'Channel updated' });
			editingId = '';
			editName = '';
			const result = await api.listChannels();
			channels = result.items || result.channels || [];
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	function cancelEditChannel() {
		editingId = '';
		editName = '';
	}

	async function toggleChannel(ch: any, nextValue: boolean) {
		const prev = ch.enabled;
		ch.enabled = nextValue;
		channels = channels;
		try {
			await api.updateChannel(ch.id, { enabled: nextValue });
			addToast({ type: 'success', message: `Channel ${ch.enabled ? 'enabled' : 'disabled'}` });
		} catch (e: any) {
			ch.enabled = prev;
			channels = channels;
			addToast({ type: 'error', message: e.message });
		}
	}

	function openDeleteConfirm(ch: any) {
		deleteConfirmId = ch.id;
		deleteConfirmName = ch.name;
		deleteConfirmOpen = true;
	}

	async function confirmDeleteChannel() {
		try {
			await api.deleteChannel(deleteConfirmId);
			channels = channels.filter(c => c.id !== deleteConfirmId);
			deleteConfirmId = '';
			deleteConfirmOpen = false;
			addToast({ type: 'success', message: 'Channel deleted' });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	function toggleEvent(event: string) {
		if (newEvents.includes(event)) {
			newEvents = newEvents.filter(e => e !== event);
		} else {
			newEvents = [...newEvents, event];
		}
	}
</script>

<Header title="Notifications" />

<ConfirmDangerModal
	bind:open={deleteConfirmOpen}
	title="Delete Notification Channel"
	message="Are you sure you want to delete &quot;{deleteConfirmName}&quot;? This cannot be undone."
	confirmText="DELETE"
	confirmLabel="Delete Channel"
	on:confirm={confirmDeleteChannel}
	on:cancel={() => { deleteConfirmOpen = false; deleteConfirmId = ''; }}
/>

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	<div class="flex items-center justify-between">
		<p class="text-sm text-text-secondary">{channels.length} notification channel{channels.length !== 1 ? 's' : ''}</p>
		<button on:click={() => showAdd = !showAdd} class="btn-primary text-sm">{showAdd ? 'Cancel' : 'Add Channel'}</button>
	</div>

	{#if showAdd}
		<div class="card">
			<h3 class="font-semibold text-text mb-4">Add Notification Channel</h3>
			<div class="space-y-4">
				<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
					<div>
						<label for="new-channel-name" class="block text-sm text-text-secondary mb-1">Name</label>
						<input id="new-channel-name" type="text" bind:value={newName} class="input" placeholder="My Discord" />
					</div>
					<div>
						<label for="new-channel-type" class="block text-sm text-text-secondary mb-1">Type</label>
						<select id="new-channel-type" bind:value={newType} class="input">
							{#each channelTypes as t}
								<option value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>
							{/each}
						</select>
					</div>
				</div>
				<div>
					<label for="new-channel-url" class="block text-sm text-text-secondary mb-1">Webhook URL</label>
					<input id="new-channel-url" type="url" bind:value={newUrl} class="input" placeholder="https://discord.com/api/webhooks/..." />
				</div>
				<div>
					<span id="new-channel-events-label" class="block text-sm text-text-secondary mb-2">Events</span>
					<div class="flex flex-wrap gap-2" role="group" aria-labelledby="new-channel-events-label">
						{#each eventOptions as event}
							<button
								on:click={() => toggleEvent(event)}
								class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium cursor-pointer transition-colors
								       {newEvents.includes(event) ? 'bg-primary/15 text-primary border border-primary/30' : 'bg-bg-surface text-text-secondary border border-border hover:border-border-strong'}"
							>{event}</button>
						{/each}
					</div>
				</div>
				<button on:click={addChannel} disabled={!newName || !newUrl} class="btn-primary disabled:opacity-50">Save</button>
			</div>
		</div>
	{/if}

	{#if loading}
		<div class="space-y-3">
			{#each Array(2) as _}
				<div class="card animate-skeleton h-20"></div>
			{/each}
		</div>
	{:else if channels.length === 0}
		<div class="card text-center py-12">
			<svg class="w-12 h-12 text-text-secondary mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" /></svg>
			<p class="text-text-secondary">No notification channels configured yet.</p>
			<button on:click={() => showAdd = true} class="btn-primary mt-4">Add Your First Channel</button>
		</div>
	{:else}
		<div class="space-y-3">
			{#each channels as ch}
				{@const ChannelIcon = getChannelIcon(ch.type)}
				<div class="card {ch.enabled === false ? 'opacity-60' : ''}">
					<div class="flex items-center justify-between">
						<div class="flex items-center gap-3">
							<div class="w-9 h-9 rounded-lg bg-bg-surface border border-border flex items-center justify-center flex-shrink-0">
								<svelte:component this={ChannelIcon} size={16} class="text-text-secondary" />
							</div>
							<div>
								{#if editingId === ch.id}
									<div class="flex items-center gap-2">
										<input type="text" bind:value={editName} class="input text-sm py-1 px-2 w-48" on:keydown={(e) => e.key === 'Enter' && saveEditChannel()} />
										<button on:click={saveEditChannel} class="btn-primary text-xs py-1 px-2">Save</button>
										<button on:click={cancelEditChannel} class="btn-secondary text-xs py-1 px-2">Cancel</button>
									</div>
								{:else}
									<div class="flex items-center gap-2">
										<p class="text-sm font-medium text-text">{ch.name}</p>
										<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-bg-surface text-text-secondary border border-border">{ch.type}</span>
										{#if ch.enabled === false}
											<span class="badge badge-warning">Disabled</span>
										{/if}
									</div>
								{/if}
								<div class="flex flex-wrap gap-1 mt-1.5">
									{#each ch.events || [] as event}
										<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-primary/10 text-primary">{event}</span>
									{/each}
								</div>
							</div>
						</div>
						<div class="flex items-center gap-3">
							<FormToggle
								checked={ch.enabled !== false}
								on:change={(e) => toggleChannel(ch, e.detail.checked)}
							/>
							<button on:click={() => testChannel(ch.id)} disabled={testing === ch.id} class="btn-secondary text-xs disabled:opacity-50">
								{testing === ch.id ? 'Sending...' : 'Test'}
							</button>
							<button on:click={() => startEditChannel(ch)} class="text-text-secondary hover:text-primary" aria-label="Edit channel">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" /></svg>
							</button>
							<button on:click={() => openDeleteConfirm(ch)} class="text-text-secondary hover:text-danger" aria-label="Delete channel">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
							</button>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</main>
