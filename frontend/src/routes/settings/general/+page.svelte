<svelte:head>
	<title>General Settings | Arkive</title>
</svelte:head>

<script lang="ts">
  import Header from '$lib/components/layout/Header.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/app';
  import { onMount } from 'svelte';
  import { settingsSchema } from '$lib/schemas/settings';
  import SectionCard from '$lib/components/ui/SectionCard.svelte';
  import FormInput from '$lib/components/ui/FormInput.svelte';
  import FormSelect from '$lib/components/ui/FormSelect.svelte';

  let serverName = '';
  let timezone = 'UTC';
  let retentionDays = 30;
  let keepDaily = 7;
  let keepWeekly = 4;
  let keepMonthly = 6;
  let logLevel = 'INFO';
  let saving = false;
  let saved = false;
  let loading = true;
  let error = '';
  let errors: Record<string, string> = {};

  // Track original values for dirty detection
  let originalValues = '';
  $: isDirty = !loading && originalValues !== JSON.stringify({ serverName, timezone, retentionDays, keepDaily, keepWeekly, keepMonthly, logLevel });

  const timezones = [
    'UTC', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Asia/Tokyo', 'Asia/Shanghai',
    'Australia/Sydney', 'Pacific/Auckland',
  ];

  const timezoneOptions = timezones.map(tz => ({ value: tz, label: tz }));
  const logLevelOptions = [
    { value: 'DEBUG', label: 'Debug' },
    { value: 'INFO', label: 'Info' },
    { value: 'WARN', label: 'Warning' },
    { value: 'ERROR', label: 'Error' },
  ];

  onMount(async () => {
    try {
      const data = await api.getSettings();
      serverName = data.server_name || '';
      timezone = data.timezone || 'UTC';
      retentionDays = data.retention_days || 30;
      keepDaily = data.keep_daily || 7;
      keepWeekly = data.keep_weekly || 4;
      keepMonthly = data.keep_monthly || 6;
      logLevel = data.log_level || 'INFO';
      originalValues = JSON.stringify({ serverName, timezone, retentionDays, keepDaily, keepWeekly, keepMonthly, logLevel });
    } catch (e: any) {
      error = e.message || 'Failed to load settings';
    }
    loading = false;
  });

  async function save() {
    const result = settingsSchema.safeParse({
      server_name: serverName, timezone,
      retention_days: retentionDays,
      keep_daily: keepDaily, keep_weekly: keepWeekly, keep_monthly: keepMonthly,
      log_level: logLevel,
    });
    if (!result.success) {
      errors = Object.fromEntries(result.error.issues.map(e => [e.path.map(String).join('.') || '_root', e.message]));
      return;
    }
    errors = {};
    saving = true;
    try {
      await api.updateSettings({
        server_name: serverName, timezone, retention_days: retentionDays,
        keep_daily: keepDaily, keep_weekly: keepWeekly, keep_monthly: keepMonthly,
        log_level: logLevel,
      });
      saved = true;
      originalValues = JSON.stringify({ serverName, timezone, retentionDays, keepDaily, keepWeekly, keepMonthly, logLevel });
      addToast({ type: 'success', message: 'Settings saved' });
      setTimeout(() => saved = false, 3000);
    } catch (e: any) {
      addToast({ type: 'error', message: e.message || 'Failed to save' });
    }
    saving = false;
  }
</script>

<Header title="General Settings" />

<div class="p-6 space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-text">General Settings</h1>
    <p class="text-sm text-text-secondary mt-1">Configure server identity and retention policies</p>
  </div>

  {#if error}
    <div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="card animate-skeleton h-64"></div>
  {:else}
    <SectionCard title="Server" description="Identity and timezone settings for this Arkive instance">
      <div class="grid grid-cols-2 gap-4">
        <FormInput
          id="server-name"
          label="Server Name"
          bind:value={serverName}
          placeholder="my-unraid-server"
        />
        <FormSelect
          id="timezone"
          label="Timezone"
          bind:value={timezone}
          options={timezoneOptions}
        />
      </div>
    </SectionCard>

    <SectionCard title="Retention Policy" description="How many snapshots to keep before pruning">
      <div class="grid grid-cols-2 gap-4">
        <FormInput
          id="keep-daily"
          label="Keep Daily"
          type="number"
          bind:value={keepDaily}
          error={errors.keep_daily}
          mono={true}
        />
        <FormInput
          id="keep-weekly"
          label="Keep Weekly"
          type="number"
          bind:value={keepWeekly}
          error={errors.keep_weekly}
          mono={true}
        />
        <FormInput
          id="keep-monthly"
          label="Keep Monthly"
          type="number"
          bind:value={keepMonthly}
          error={errors.keep_monthly}
          mono={true}
        />
        <FormInput
          id="retention-days"
          label="Max Retention Days"
          type="number"
          bind:value={retentionDays}
          error={errors.retention_days}
          mono={true}
        />
      </div>
    </SectionCard>

    <SectionCard title="Logging" description="Control verbosity of application logs">
      <div class="max-w-xs">
        <FormSelect
          id="log-level"
          label="Log Level"
          bind:value={logLevel}
          options={logLevelOptions}
        />
      </div>
    </SectionCard>

    <div class="flex items-center gap-3">
      <button on:click={save} disabled={saving} class="btn-primary disabled:opacity-50">
        {#if saving}Saving...{:else}Save Settings{/if}
      </button>
      {#if saved}
        <span class="text-sm text-success">Settings saved</span>
      {/if}
      {#if isDirty}
        <span class="flex items-center gap-1.5 text-xs text-warning">
          <span class="w-1.5 h-1.5 rounded-full bg-warning"></span>
          Unsaved changes
        </span>
      {/if}
    </div>
  {/if}
</div>
