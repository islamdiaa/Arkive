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
  import { Button } from '$lib/components/ui/button';
  import { Input } from '$lib/components/ui/input';
  import NotificationEventPicker from '$lib/components/shared/NotificationEventPicker.svelte';
  import { loadNotificationEvents } from '$lib/api/notifications';
  import { fallbackEventCatalog, partitionEvents } from '$lib/utils/notification-events';
  import type { NotificationEventCatalog } from '$lib/types';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  let name: string = '';
  let type: string = 'discord';
  let url: string = '';
  let loading: boolean = false;
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

  const channelTypes: Array<{ value: string; label: string }> = [
    { value: 'discord', label: 'Discord' },
    { value: 'slack', label: 'Slack' },
    { value: 'email', label: 'Email' },
    { value: 'pushover', label: 'Pushover' },
    { value: 'telegram', label: 'Telegram' },
    { value: 'ntfy', label: 'ntfy' },
    { value: 'gotify', label: 'Gotify' },
    { value: 'webhook', label: 'Webhook' },
    { value: 'uptimekuma', label: 'Uptime Kuma' },
  ];

  $: urlPlaceholder = getUrlPlaceholder(type);
  $: urlLabel = getUrlLabel(type);
  $: canSubmit = name.trim().length > 0 && url.trim().length > 0 && events.length > 0;
  $: if (!open) resetForm();

  function getUrlPlaceholder(t: string): string {
    switch (t) {
      case 'discord':
        return 'https://discord.com/api/webhooks/...';
      case 'slack':
        return 'https://hooks.slack.com/services/...';
      case 'email':
        return 'smtp://user:pass@smtp.example.com:587';
      case 'pushover':
        return 'https://api.pushover.net/1/messages.json';
      case 'telegram':
        return 'https://api.telegram.org/bot<token>/sendMessage';
      case 'ntfy':
        return 'https://ntfy.sh/your-topic';
      case 'gotify':
        return 'https://gotify.example.com/message?token=...';
      case 'webhook':
        return 'https://your-server.com/webhook';
      case 'uptimekuma':
        return 'https://uptime.example.com/api/push/...';
      default:
        return 'https://...';
    }
  }

  function getUrlLabel(t: string): string {
    switch (t) {
      case 'email':
        return 'SMTP Connection URL';
      case 'discord':
      case 'slack':
        return 'Webhook URL';
      default:
        return 'URL / Endpoint';
    }
  }

  function resetForm() {
    name = '';
    type = 'discord';
    url = '';
    loading = false;
    events = [...catalog.defaults];
    eventsTouched = false;
  }

  function handleSave() {
    if (!canSubmit) return;
    loading = true;
    // Only send values the backend accepts (unknown events are rejected with 422).
    dispatch('save', { name, type, url, events: partitionEvents(events, catalog).kept, enabled: true });
  }

  function handleCancel() {
    open = false;
  }

  /** Called by parent to clear loading state */
  export function setDone() {
    loading = false;
  }
</script>

<Dialog bind:open>
  <DialogContent class="max-w-lg">
    <DialogHeader>
      <DialogTitle>Add Notification Channel</DialogTitle>
      <DialogDescription>
        Set up a notification channel to receive alerts about backup events.
      </DialogDescription>
    </DialogHeader>

    <div class="space-y-4 py-4">
      <!-- Channel name -->
      <div class="space-y-1.5">
        <label class="text-sm font-medium text-text-primary" for="channel-name">Name</label>
        <Input id="channel-name" bind:value={name} placeholder="My Discord Alerts" />
      </div>

      <!-- Channel type -->
      <div class="space-y-1.5">
        <span id="channel-type-label" class="text-sm font-medium text-text-primary">Type</span>
        <div class="grid grid-cols-3 gap-2" role="group" aria-labelledby="channel-type-label">
          {#each channelTypes as ct (ct.value)}
            <button
              type="button"
              class="rounded-md border px-3 py-2 text-sm transition-colors
                {type === ct.value
                  ? 'border-primary bg-primary/10 text-text-primary font-medium'
                  : 'border-border bg-bg-input text-text-secondary hover:bg-bg-surface-hover'}"
              on:click={() => (type = ct.value)}
            >
              {ct.label}
            </button>
          {/each}
        </div>
      </div>

      <!-- URL / Webhook -->
      <div class="space-y-1.5">
        <label class="text-sm font-medium text-text-primary" for="channel-url">{urlLabel}</label>
        <Input id="channel-url" bind:value={url} placeholder={urlPlaceholder} class="font-mono" />
      </div>

      <!-- Events -->
      <div class="space-y-1">
        <NotificationEventPicker
          {catalog}
          bind:selected={events}
          fromFallback={catalogFromFallback}
          idPrefix="channel-events"
          on:change={() => (eventsTouched = true)}
        />
        <p class="text-xs text-text-muted">Select which events should trigger a notification.</p>
      </div>
    </div>

    <DialogFooter>
      <Button variant="outline" on:click={handleCancel}>Cancel</Button>
      <Button on:click={handleSave} disabled={loading || !canSubmit}>
        {#if loading}
          <svg class="mr-2 h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
          Saving...
        {:else}
          Add Channel
        {/if}
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
