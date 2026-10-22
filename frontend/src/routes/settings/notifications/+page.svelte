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
	import NotificationEventPicker from '$lib/components/shared/NotificationEventPicker.svelte';
	import { loadNotificationEvents } from '$lib/api/notifications';
	import {
		eventLabel,
		fallbackEventCatalog,
		partitionEvents,
		WILDCARD_EVENT,
	} from '$lib/utils/notification-events';
	import type { NotificationEventCatalog } from '$lib/types';

	let channels: any[] = [];
	let loading = true;
	let error = '';
	let showAdd = false;
	let testing = '';
	let editingId = '';
	let editName = '';
	let editEvents: string[] = [];
	let editDropped: string[] = [];
	let savingEdit = false;
	let deleteConfirmId = '';
	let deleteConfirmOpen = false;
	let deleteConfirmName = '';

	let newName = '';
	let newType = 'discord';
	let newUrl = '';
	// Event catalog from GET /api/notifications/events; the static copy is used
	// until it loads (and if the request fails).
	let catalog: NotificationEventCatalog = fallbackEventCatalog();
	let catalogFromFallback = false;
	let newEvents: string[] = [...catalog.defaults];

	// Must match NotificationChannelCreate.type in backend/app/models/notifications.py
	const channelTypes = ['discord', 'slack', 'telegram', 'email', 'ntfy', 'gotify', 'pushover', 'webhook', 'uptimekuma'];

	$: knownEvents = new Set([...catalog.events.map((e) => e.value), WILDCARD_EVENT]);

	function getChannelIcon(type: string) {
		if (type === 'discord') return MessageSquare;
		if (type === 'slack') return Hash;
		if (type === 'ntfy') return Bell;
		if (type === 'email') return Mail;
		return Webhook;
	}

	onMount(async () => {
		const eventsPromise = loadNotificationEvents();
		try {
			const result = await api.listChannels();
			channels = result.items || result.channels || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load notification channels';
		}
		const loaded = await eventsPromise;
		catalog = { events: loaded.events, defaults: loaded.defaults };
		catalogFromFallback = loaded.fromFallback;
		newEvents = [...catalog.defaults];
		loading = false;
	});

	function openAddForm() {
		showAdd = !showAdd;
		if (showAdd) newEvents = [...catalog.defaults];
	}

	async function addChannel() {
		// Only send values the backend accepts (it rejects unknown events with 422).
		const events = partitionEvents(newEvents, catalog).kept;
		try {
			await api.createChannel({ name: newName, type: newType, url: newUrl, events, enabled: true });
			addToast({ type: 'success', message: `Channel "${newName}" added` });
			showAdd = false;
			newName = '';
			newUrl = '';
			newEvents = [...catalog.defaults];
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
		// Legacy event values (from older Arkive versions) are dropped on save;
		// the picker lists them so the change is visible.
		const { kept, dropped } = partitionEvents(ch.events, catalog);
		editDropped = dropped;
		editEvents = kept.length > 0 ? kept : dropped.length > 0 ? [...catalog.defaults] : [];
	}

	async function saveEditChannel() {
		if (!editName.trim() || editEvents.length === 0 || savingEdit) return;
		savingEdit = true;
		const events = partitionEvents(editEvents, catalog).kept;
		const dropped = editDropped;
		try {
			await api.updateChannel(editingId, { name: editName, events });
			addToast({
				type: 'success',
				message: dropped.length
					? `Channel updated. Removed unsupported events: ${dropped.join(', ')}`
					: 'Channel updated',
			});
			cancelEditChannel();
			const result = await api.listChannels();
			channels = result.items || result.channels || [];
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		savingEdit = false;
	}

	function cancelEditChannel() {
		editingId = '';
		editName = '';
		editEvents = [];
		editDropped = [];
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
		<button on:click={openAddForm} class="btn-primary text-sm">{showAdd ? 'Cancel' : 'Add Channel'}</button>
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
				<NotificationEventPicker
					{catalog}
					bind:selected={newEvents}
					fromFallback={catalogFromFallback}
					idPrefix="new-channel-events"
					compact={true}
				/>
				<button on:click={addChannel} disabled={!newName || !newUrl || newEvents.length === 0} class="btn-primary disabled:opacity-50">Save</button>
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
			<button on:click={() => { showAdd = true; newEvents = [...catalog.defaults]; }} class="btn-primary mt-4">Add Your First Channel</button>
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
										<input type="text" bind:value={editName} aria-label="Channel name" class="input text-sm py-1 px-2 w-48" on:keydown={(e) => e.key === 'Enter' && saveEditChannel()} />
										<button on:click={saveEditChannel} disabled={!editName.trim() || editEvents.length === 0 || savingEdit} class="btn-primary text-xs py-1 px-2 disabled:opacity-50">{savingEdit ? 'Saving...' : 'Save'}</button>
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
								{#if editingId !== ch.id}
									<div class="flex flex-wrap gap-1 mt-1.5">
										{#each ch.events || [] as event}
											{#if knownEvents.has(event)}
												<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-primary-bg text-primary" title={event}>{eventLabel(event, catalog)}</span>
											{:else}
												<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-bg-surface text-text-muted border border-border line-through" title="No longer sent by Arkive — removed when you next edit this channel">{event}</span>
											{/if}
										{/each}
									</div>
								{/if}
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
					{#if editingId === ch.id}
						<div class="mt-4 pt-4 border-t border-border-muted">
							<NotificationEventPicker
								{catalog}
								bind:selected={editEvents}
								dropped={editDropped}
								fromFallback={catalogFromFallback}
								idPrefix="edit-channel-events-{ch.id}"
								compact={true}
							/>
							{#if editEvents.length === 0}
								<p class="mt-2 text-xs text-danger">Select at least one event.</p>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</main>
