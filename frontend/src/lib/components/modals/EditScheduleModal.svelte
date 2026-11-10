<script lang="ts">
  import { createEventDispatcher } from 'svelte';
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
  import { cronRegex } from '$lib/schemas/schedule';

  export let open: boolean = false;
  export let job: { id: string; name: string; schedule: string } | null = null;

  const dispatch = createEventDispatcher();

  let schedule: string = '0 7 * * *';
  let loading: boolean = false;
  let errors: Record<string, string> = {};

  const presets: Array<{ label: string; value: string }> = [
    { label: 'Every 6 hours', value: '0 */6 * * *' },
    { label: 'Every 12 hours', value: '0 */12 * * *' },
    { label: 'Daily at 3 AM', value: '0 3 * * *' },
    { label: 'Daily at 7 AM', value: '0 7 * * *' },
    { label: 'Twice daily', value: '0 3,15 * * *' },
    { label: 'Weekly (Sunday 3 AM)', value: '0 3 * * 0' },
    { label: 'Monthly (1st at 3 AM)', value: '0 3 1 * *' },
  ];

  /** Cron field labels for the 5-part display */
  const cronFields = ['Minute', 'Hour', 'Day', 'Month', 'Weekday'];

  $: cronParts = schedule.split(/\s+/).slice(0, 5);
  $: while (cronParts.length < 5) cronParts.push('*');
  $: cronDisplay = cronParts.map((part, i) => ({ label: cronFields[i], value: part }));

  $: if (open && job) {
    schedule = job.schedule || '0 7 * * *';
  }
  $: if (!open) {
    loading = false;
  }

  $: humanReadable = describeCron(schedule);

  function describeCron(cron: string): string {
    const parts = cron.trim().split(/\s+/);
    if (parts.length < 5) return 'Invalid cron expression';

    const [min, hour, dom, mon, dow] = parts;

    // Check common patterns
    if (min === '0' && hour.startsWith('*/')) {
      return `Every ${hour.replace('*/', '')} hours`;
    }
    if (min === '0' && hour.includes(',')) {
      return `Daily at ${hour.split(',').map((h) => `${h}:00`).join(' and ')}`;
    }
    if (dom === '1' && mon === '*' && dow === '*' && min === '0') {
      return `Monthly on the 1st at ${hour}:00`;
    }
    if (dow !== '*' && dom === '*' && mon === '*') {
      const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      const dayName = days[parseInt(dow)] || dow;
      return `Weekly on ${dayName} at ${hour}:${min.padStart(2, '0')}`;
    }
    if (dom === '*' && mon === '*' && dow === '*') {
      if (hour === '*' && min.startsWith('*/')) {
        return `Every ${min.replace('*/', '')} minutes`;
      }
      if (hour !== '*') {
        return `Daily at ${hour}:${min.padStart(2, '0')}`;
      }
    }

    return cron;
  }

  function selectPreset(presetValue: string) {
    schedule = presetValue;
  }

  function updateCronPart(index: number, value: string) {
    const parts = schedule.split(/\s+/);
    while (parts.length < 5) parts.push('*');
    parts[index] = value || '*';
    schedule = parts.slice(0, 5).join(' ');
  }

  function handleSave() {
    if (!job) return;
    if (!cronRegex.test(schedule)) {
      errors = { schedule: 'Invalid cron expression' };
      return;
    }
    errors = {};
    loading = true;
    dispatch('save', { jobId: job.id, schedule });
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
      <DialogTitle>Edit Schedule</DialogTitle>
      <DialogDescription>
        {#if job}
          Configure the backup schedule for <strong class="text-text-primary">{job.name}</strong>.
        {:else}
          Configure the backup schedule.
        {/if}
      </DialogDescription>
    </DialogHeader>

    <div class="space-y-5 py-4">
      <!-- Quick presets -->
      <div class="space-y-2">
        <span id="presets-label" class="text-sm font-medium text-text-primary">Quick Presets</span>
        <div class="flex flex-wrap gap-1.5" role="group" aria-labelledby="presets-label">
          {#each presets as preset (preset.value)}
            <button
              type="button"
              class="rounded-md border px-2.5 py-1 text-xs transition-colors
                {schedule === preset.value
                  ? 'border-primary bg-primary/10 text-primary font-medium'
                  : 'border-border text-text-secondary hover:text-text-primary hover:border-primary/50'}"
              on:click={() => selectPreset(preset.value)}
            >
              {preset.label}
            </button>
          {/each}
        </div>
      </div>

      <!-- Cron expression input -->
      <div class="space-y-1.5">
        <label class="text-sm font-medium text-text-primary" for="cron-input">Cron Expression</label>
        <Input id="cron-input" bind:value={schedule} placeholder="* * * * *" class="font-mono" />
        <p class="text-xs text-text-muted">
          Format: minute hour day-of-month month day-of-week
        </p>
        {#if errors.schedule}<p class="text-xs text-danger mt-1">{errors.schedule}</p>{/if}
      </div>

      <!-- Cron field breakdown -->
      <div class="grid grid-cols-5 gap-2">
        {#each cronDisplay as field, i (field.label)}
          <div class="space-y-1">
            <label for="cron-field-{i}" class="block text-center text-[10px] font-medium uppercase tracking-wider text-text-muted">
              {field.label}
            </label>
            <input
              id="cron-field-{i}"
              type="text"
              value={field.value}
              on:input={(e) => updateCronPart(i, e.currentTarget.value)}
              class="w-full rounded-md border border-border bg-bg-input px-2 py-1.5 text-center text-sm font-mono text-text-primary
                focus:outline-hidden focus:ring-2 focus:ring-primary focus:ring-offset-1 focus:ring-offset-bg-app"
            />
          </div>
        {/each}
      </div>

      <!-- Human-readable description -->
      <div class="rounded-md border border-border bg-bg-elevated px-3 py-2">
        <p class="text-sm text-text-secondary">
          <span class="font-medium text-text-primary">Runs:</span>
          {humanReadable}
        </p>
      </div>
    </div>

    <DialogFooter>
      <Button variant="outline" on:click={handleCancel}>Cancel</Button>
      <Button on:click={handleSave} disabled={loading || !job}>
        {#if loading}
          <svg class="mr-2 h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
          </svg>
          Saving...
        {:else}
          Save Schedule
        {/if}
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
