<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
    DialogFooter,
  } from '$lib/components/ui/dialog';
  import NotificationEventPicker from '$lib/components/shared/NotificationEventPicker.svelte';
  import { loadNotificationEvents } from '$lib/api/notifications';
  import { fallbackEventCatalog, partitionEvents } from '$lib/utils/notification-events';
  import type { NotificationEventCatalog } from '$lib/types';

  export let open: boolean = false;
  export let loading: boolean = false;

  const dispatch = createEventDispatcher();

  let name = '';
  let type = 'slack';
  let url = '';
  // Catalog from GET /api/notifications/events; built-in copy until it loads.
  let catalog: NotificationEventCatalog = fallbackEventCatalog();
  let catalogFromFallback = false;
  let events: string[] = [...catalog.defaults];
  let eventsTouched = false;

  onMount(async () => {
    const loaded = await loadNotificationEvents();
    catalog = { events: loaded.events, defaults: loaded.defaults };
    catalogFromFallback = loaded.fromFallback;
    if (!eventsTouched) events = [...catalog.defaults];
  });

  const types = [
    { value: 'slack', label: 'Slack' },
    { value: 'discord', label: 'Discord' },
    { value: 'telegram', label: 'Telegram' },
    { value: 'email', label: 'Email' },
    { value: 'ntfy', label: 'ntfy' },
    { value: 'gotify', label: 'Gotify' },
    { value: 'pushover', label: 'Pushover' },
    { value: 'webhook', label: 'Webhook' },
    { value: 'uptimekuma', label: 'Uptime Kuma' },
  ];

  function submit() {
    // Only send values the backend accepts (unknown events are rejected with 422).
    dispatch('submit', { name, type, url, events: partitionEvents(events, catalog).kept, enabled: true });
  }

  function cancel() { dispatch('cancel'); open = false; }
</script>

<Dialog bind:open>
  <DialogContent class="max-w-lg">
    <DialogHeader>
      <DialogTitle>Add Notification Channel</DialogTitle>
      <DialogDescription>Configure a new notification destination for backup events.</DialogDescription>
    </DialogHeader>

    <div class="space-y-4 py-2">
      <div>
        <label for="notif-name" class="text-sm font-medium text-text block mb-1">Name</label>
        <input id="notif-name" bind:value={name} class="w-full bg-page border border-border rounded-md px-3 py-2 text-sm text-text" placeholder="My Slack Channel" />
      </div>
      <div>
        <label for="notif-type" class="text-sm font-medium text-text block mb-1">Type</label>
        <select id="notif-type" bind:value={type} class="w-full bg-page border border-border rounded-md px-3 py-2 text-sm text-text">
          {#each types as t}
            <option value={t.value}>{t.label}</option>
          {/each}
        </select>
      </div>
      <div>
        <label for="notif-url" class="text-sm font-medium text-text block mb-1">URL / Webhook</label>
        <input id="notif-url" bind:value={url} class="w-full bg-page border border-border rounded-md px-3 py-2 text-sm text-text font-mono" placeholder="https://hooks.slack.com/..." />
      </div>
      <NotificationEventPicker
        {catalog}
        bind:selected={events}
        fromFallback={catalogFromFallback}
        idPrefix="notif-events"
        on:change={() => (eventsTouched = true)}
      />
    </div>

    <DialogFooter>
      <button on:click={cancel} class="px-4 py-2 text-sm rounded-md border border-border text-text-secondary hover:text-text transition-colors">Cancel</button>
      <button on:click={submit} disabled={loading || !name || !url || events.length === 0} class="px-4 py-2 text-sm rounded-md bg-primary text-white font-medium hover:bg-primary/90 transition-colors disabled:opacity-50">
        {#if loading}<span class="animate-spin mr-1">⟳</span>{/if}
        Add Channel
      </button>
    </DialogFooter>
  </DialogContent>
</Dialog>
