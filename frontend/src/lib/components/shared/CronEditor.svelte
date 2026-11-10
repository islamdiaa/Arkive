<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { getCronPreview } from '$lib/api/schedule';

  export let value: string = '0 7 * * *';
  export let label: string = 'Schedule';

  const dispatch = createEventDispatcher();

  let nextRuns: string[] = [];
  let previewLoading = false;
  let previewError = '';
  let debounceTimer: ReturnType<typeof setTimeout>;

  const presets = [
    { label: 'Every 6 hours', value: '0 */6 * * *' },
    { label: 'Every 12 hours', value: '0 */12 * * *' },
    { label: 'Daily at 3 AM', value: '0 3 * * *' },
    { label: 'Daily at 7 AM', value: '0 7 * * *' },
    { label: 'Weekly (Sunday)', value: '0 3 * * 0' },
  ];

  async function fetchPreview() {
    if (!value || value.trim().split(/\s+/).length < 5) {
      nextRuns = [];
      return;
    }
    previewLoading = true;
    previewError = '';
    try {
      const result = await getCronPreview(value);
      nextRuns = result.next_runs || [];
    } catch (e) {
      previewError = 'Invalid cron expression';
      nextRuns = [];
    } finally {
      previewLoading = false;
    }
  }

  function debouncedPreview() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchPreview, 500);
  }

  function selectPreset(preset: string) {
    value = preset;
    dispatch('change', value);
    debouncedPreview();
  }

  function handleInput() {
    dispatch('change', value);
    debouncedPreview();
  }

  onMount(fetchPreview);
</script>

<div class="space-y-2">
  <label for="cron-editor-input" class="text-sm font-medium text-text">{label}</label>
  <input
    id="cron-editor-input"
    type="text"
    bind:value
    on:input={handleInput}
    class="mt-2 w-full bg-bg-input border border-border rounded-md px-3 py-2 text-sm font-mono text-text focus:border-primary focus:ring-1 focus:ring-primary outline-hidden"
    placeholder="* * * * *"
  />
  <div class="flex flex-wrap gap-1">
    {#each presets as preset}
      <button
        on:click={() => selectPreset(preset.value)}
        class="text-xs px-2 py-1 rounded border text-text-secondary hover:text-primary hover:border-primary transition-colors
               {value === preset.value ? 'border-primary text-primary bg-primary/5' : 'border-border bg-bg-surface'}"
      >
        {preset.label}
      </button>
    {/each}
  </div>
  {#if previewLoading}
    <p class="text-xs text-text-secondary mt-2">Loading preview...</p>
  {:else if previewError}
    <p class="text-xs text-danger mt-2">{previewError}</p>
  {:else if nextRuns.length > 0}
    <div class="mt-2 space-y-0.5">
      <p class="text-xs text-text-secondary">Next runs:</p>
      {#each nextRuns as run}
        <p class="text-xs text-text font-mono">{new Date(run).toLocaleString()}</p>
      {/each}
    </div>
  {/if}
</div>
