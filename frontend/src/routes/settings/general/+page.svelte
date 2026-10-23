<svelte:head>
	<title>General Settings | Arkive</title>
</svelte:head>

<script lang="ts">
  import Header from '$lib/components/layout/Header.svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/app';
  import { onMount, tick } from 'svelte';
  import { settingsSchema } from '$lib/schemas/settings';
  import SectionCard from '$lib/components/ui/SectionCard.svelte';
  import FormInput from '$lib/components/ui/FormInput.svelte';
  import FormSelect from '$lib/components/ui/FormSelect.svelte';
  import FormToggle from '$lib/components/ui/FormToggle.svelte';
  import CronEditor from '$lib/components/shared/CronEditor.svelte';
  import { saveSettings } from '$lib/api/settings';
  import { describeCron } from '$lib/utils/schedule';

  let serverName = '';
  let timezone = 'UTC';
  let retentionDays = 30;
  let keepDaily = 7;
  let keepWeekly = 4;
  let keepMonthly = 6;
  let logLevel = 'INFO';
  let dumpCompression = 'none';
  let drillEnabled = true;
  let drillSchedule = '0 7 * * 0';
  let drillMaxDatabases: number | string = 3;
  let drillMaxDumpMb: number | string = 2048;
  let saving = false;
  let saved = false;
  let loading = true;
  let error = '';
  let errors: Record<string, string> = {};

  // Track original values for dirty detection
  let originalValues = '';
  function snapshot(): string {
    return JSON.stringify({
      serverName, timezone, retentionDays, keepDaily, keepWeekly, keepMonthly, logLevel,
      dumpCompression, drillEnabled, drillSchedule, drillMaxDatabases, drillMaxDumpMb,
    });
  }
  $: currentValues = JSON.stringify({
    serverName, timezone, retentionDays, keepDaily, keepWeekly, keepMonthly, logLevel,
    dumpCompression, drillEnabled, drillSchedule, drillMaxDatabases, drillMaxDumpMb,
  });
  $: isDirty = !loading && originalValues !== currentValues;

  // Human-readable drill schedule, hidden when it would just repeat the cron string.
  $: drillScheduleText = describeCron(drillSchedule) === drillSchedule.trim().replace(/\s+/g, ' ')
    ? ''
    : describeCron(drillSchedule);

  const dumpCompressionOptions = [
    { value: 'none', label: 'Uncompressed — recommended: restic compresses and deduplicates, much smaller daily repository growth' },
    { value: 'gzip', label: 'gzip — smaller local dump folder, larger repository growth' },
  ];

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
      dumpCompression = data.dump_compression === 'gzip' ? 'gzip' : 'none';
      drillEnabled = data.restore_drill_enabled !== false && data.restore_drill_enabled !== 'false';
      drillSchedule = data.restore_drill_schedule || '0 7 * * 0';
      drillMaxDatabases = Number(data.restore_drill_max_databases) || 3;
      drillMaxDumpMb = Number(data.restore_drill_max_dump_mb) || 2048;
      originalValues = snapshot();
    } catch (e: any) {
      error = e.message || 'Failed to load settings';
    }
    loading = false;
    // Deep link from the dashboard's drills panel (/settings/general#restore-drills):
    // the settings layout scrolls an inner container, so scroll the section into view.
    if (window.location.hash === '#restore-drills') {
      await tick();
      document.getElementById('restore-drills')?.scrollIntoView({ block: 'start' });
    }
  });

  async function save() {
    const values = {
      server_name: serverName, timezone,
      retention_days: retentionDays,
      keep_daily: keepDaily, keep_weekly: keepWeekly, keep_monthly: keepMonthly,
      log_level: logLevel,
      dump_compression: dumpCompression,
      restore_drill_enabled: drillEnabled,
      restore_drill_schedule: drillSchedule.trim(),
      // Empty number inputs become 0/NaN here so validation rejects them.
      restore_drill_max_databases: Number(drillMaxDatabases),
      restore_drill_max_dump_mb: Number(drillMaxDumpMb),
    };
    const result = settingsSchema.safeParse(values);
    if (!result.success) {
      errors = Object.fromEntries(result.error.issues.map(e => [e.path.map(String).join('.') || '_root', e.message]));
      const first = result.error.issues[0];
      addToast({ type: 'error', message: `${first.path.map(String).join('.') || 'Settings'}: ${first.message}` });
      return;
    }
    errors = {};
    saving = true;
    try {
      // Bulk PUT /api/settings with string values ({settings: {...}}).
      await saveSettings(values);
      saved = true;
      originalValues = snapshot();
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

    <SectionCard title="Database Dumps" description="How Arkive writes database dumps before each backup">
      <FormSelect
        id="dump-compression"
        label="Dump Compression"
        bind:value={dumpCompression}
        options={dumpCompressionOptions}
      />
      <p class="text-xs text-text-secondary">
        Uncompressed dumps let restic deduplicate unchanged data between days, so each new snapshot only stores what changed. gzip scrambles the whole file on every change, so each backup uploads nearly the full dump again. Existing dumps in either format stay restorable.
      </p>
    </SectionCard>

    <div id="restore-drills" class="scroll-mt-6">
      <SectionCard title="Restore Drills" description="Prove your database dumps actually restore, not just that they exist">
        <p class="text-sm text-text-secondary">
          A drill restores recent database dumps from the latest snapshot and loads them into a temporary, network-isolated copy of each database's own container image to prove they restore. Results appear on the dashboard, and a failed drill sends a “Restore drill failed” notification.
        </p>
        <FormToggle
          id="restore-drill-enabled"
          checked={drillEnabled}
          label={drillEnabled ? 'Scheduled drills enabled' : 'Scheduled drills disabled'}
          description="You can always run a drill manually from the dashboard"
          on:change={(e) => (drillEnabled = e.detail.checked)}
        />
        <div class={drillEnabled ? '' : 'opacity-60'}>
          <CronEditor bind:value={drillSchedule} label="Drill schedule" />
          {#if drillScheduleText}
            <p class="text-xs text-text-secondary mt-1">{drillScheduleText}</p>
          {/if}
        </div>
        <div class="grid grid-cols-2 gap-4">
          <FormInput
            id="restore-drill-max-databases"
            label="Databases per drill"
            type="number"
            bind:value={drillMaxDatabases}
            error={errors.restore_drill_max_databases}
            hint="How many databases each drill restores (1–100)"
            mono={true}
          />
          <FormInput
            id="restore-drill-max-dump-mb"
            label="Max dump size (MB)"
            type="number"
            bind:value={drillMaxDumpMb}
            error={errors.restore_drill_max_dump_mb}
            hint="Larger dumps are skipped to keep drills quick"
            mono={true}
          />
        </div>
        {#if errors.restore_drill_schedule}
          <p class="text-xs text-danger" role="alert">{errors.restore_drill_schedule}</p>
        {/if}
      </SectionCard>
    </div>

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
