<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Header from '$lib/components/layout/Header.svelte';
  import { api } from '$lib/api/client';
  import { backupApi } from '$lib/api/backup';
  import { StatusBadge, LogViewer } from '$lib/components/shared';
  import { formatBytes, formatDuration } from '$lib/utils/format';
  import { formatDateTime } from '$lib/utils/date';
  import { subscribe as sseSubscribe } from '$lib/stores/sse';
  import { BACKUP_PHASES } from '$lib/constants/backup-phases';
  import { jobTypeLabel } from '$lib/utils/job-types';
  import { statusTone } from '$lib/utils/run-status';
  import type { LogEntry } from '$lib/types';

  export let data: { runId: string };

  interface RunTarget {
    id: number;
    target_id: string;
    target_name?: string | null;
    target_provider?: string | null;
    status: string;
    snapshot_id?: string | null;
    upload_bytes?: number | null;
    duration_seconds?: number | null;
    error?: string | null;
  }

  interface RunDatabase {
    id: number;
    container_name: string;
    db_name: string;
    db_type: string;
    status: string;
    dump_size_bytes?: number | null;
    integrity_check?: string | null;
    host_path?: string | null;
    error?: string | null;
  }

  let run: Record<string, any> | null = null;
  let loading = true;
  let error = '';
  let logs: LogEntry[] = [];
  let logsLoading = false;
  let livePhase: string | null = null;
  let livePercent: number | null = null;

  $: targets = (run?.targets ?? []) as RunTarget[];
  $: databases = (run?.databases ?? []) as RunDatabase[];
  $: dbCounts = {
    success: databases.filter(d => d.status === 'success').length,
    failed: databases.filter(d => d.status === 'failed').length,
    skipped: databases.filter(d => d.status === 'skipped').length,
  };
  $: targetsOk = targets.filter(t => t.status === 'success').length;
  $: problemTone = statusTone(run?.status);
  $: livePhaseLabel = livePhase
    ? BACKUP_PHASES.find(p => p.key === livePhase?.split(':')[0])?.label ?? livePhase
    : null;

  $: durationDisplay = (() => {
    if (!run) return '--';
    if (run.duration_seconds != null) return formatDuration(run.duration_seconds);
    if (run.started_at && run.status === 'running') {
      const elapsed = Math.floor((Date.now() - new Date(run.started_at).getTime()) / 1000);
      return formatDuration(Math.max(elapsed, 0)) + ' (running)';
    }
    return '--';
  })();

  async function loadRun() {
    error = '';
    try {
      run = (await backupApi.getRun(data.runId)) as any;
      if (run?.status !== 'running') {
        livePhase = null;
        livePercent = null;
      }
    } catch (err: any) {
      error = err.message || 'Failed to load backup run';
    }
    loading = false;
  }

  async function loadLogs() {
    logsLoading = true;
    try {
      const res = await api.get<{ items: LogEntry[] }>(`/jobs/runs/${data.runId}/logs`);
      logs = res.items || [];
    } catch {
      // Logs may not be available for all runs
      logs = [];
    }
    logsLoading = false;
  }

  function refreshAll() {
    loadRun();
    loadLogs();
  }

  const isThisRun = (event: { data: Record<string, unknown> }) => event.data.run_id === data.runId;

  const unsubs = [
    sseSubscribe('backup:progress', (event) => {
      if (!isThisRun(event)) return;
      livePhase = (event.data.phase as string) ?? livePhase;
      livePercent = typeof event.data.percent === 'number' ? (event.data.percent as number) : livePercent;
    }),
    sseSubscribe('backup:completed', (event) => { if (isThisRun(event)) refreshAll(); }),
    sseSubscribe('backup:failed', (event) => { if (isThisRun(event)) refreshAll(); }),
    sseSubscribe('backup:cancelled', (event) => { if (isThisRun(event)) refreshAll(); }),
  ];

  onMount(() => {
    refreshAll();
  });

  onDestroy(() => {
    unsubs.forEach((unsub) => unsub());
  });

  const problemBoxClass: Record<string, string> = {
    danger: 'border-danger/30 bg-danger/5 text-danger',
    warning: 'border-warning/30 bg-warning/5 text-warning',
    neutral: 'border-border bg-bg-elevated text-text-secondary',
    info: 'border-border bg-bg-elevated text-text-secondary',
    success: 'border-border bg-bg-elevated text-text-secondary',
  };
</script>

<svelte:head>
  <title>Backup Run {data.runId?.substring(0, 8)} | Arkive</title>
</svelte:head>

<Header title="Run Details" />

<main class="p-6 space-y-6">
  <!-- Breadcrumb & Back -->
  <div class="flex items-center gap-2">
    <a
      href="/backups"
      class="flex items-center gap-1 text-sm text-text-secondary hover:text-text transition-colors"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>
      Back to Backups
    </a>
    <span class="text-text-muted">/</span>
    <span class="text-sm text-text-muted font-mono">{data.runId?.substring(0, 12)}</span>
  </div>

  {#if loading}
    <div class="flex flex-col items-center justify-center py-20 gap-3 text-text-secondary">
      <svg class="w-6 h-6 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
      </svg>
      <span class="text-sm">Loading run details...</span>
    </div>
  {:else if error && !run}
    <div class="flex flex-col items-center justify-center py-20 gap-3 text-danger">
      <span class="text-sm">{error}</span>
      <button
        on:click={() => { loading = true; loadRun(); }}
        class="px-4 py-1.5 text-sm bg-bg-elevated border border-border rounded-md text-text hover:bg-bg-surface-hover"
      >
        Retry
      </button>
    </div>
  {:else if run}
    {#if error}
      <div class="p-3 bg-danger/10 border border-danger/30 rounded text-danger text-sm">Refresh failed: {error}</div>
    {/if}

    <!-- Run Header -->
    <div class="bg-bg-surface border border-border rounded-lg p-6">
      <div class="flex items-start justify-between gap-4 mb-4">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-3 mb-1">
            <h1 class="text-lg font-semibold text-text">{run.job_name || 'Backup Run'}</h1>
            <StatusBadge status={run.status} />
          </div>
          <p class="text-sm text-text-muted font-mono">{run.id}</p>
          {#if run.job_type}
            <p class="text-xs text-text-secondary mt-1">{jobTypeLabel(run.job_type)}</p>
          {/if}
        </div>
        <span class="text-xs px-2 py-0.5 rounded bg-bg-elevated text-text-secondary capitalize shrink-0">
          {run.trigger}
        </span>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <div class="text-xs text-text-muted mb-1">Started</div>
          <div class="text-sm text-text">{run.started_at ? formatDateTime(run.started_at) : '--'}</div>
        </div>
        <div>
          <div class="text-xs text-text-muted mb-1">Completed</div>
          <div class="text-sm text-text">
            {run.completed_at ? formatDateTime(run.completed_at) : (run.status === 'running' ? 'In progress' : '--')}
          </div>
        </div>
        <div>
          <div class="text-xs text-text-muted mb-1">Duration</div>
          <div class="text-sm text-text">{durationDisplay}</div>
        </div>
        <div>
          <div class="text-xs text-text-muted mb-1">Total Size</div>
          <div class="text-sm text-text">{run.total_size_bytes ? formatBytes(run.total_size_bytes) : '--'}</div>
        </div>
      </div>

      {#if run.status === 'running'}
        <p class="mt-4 text-sm text-text-secondary">
          {#if livePhaseLabel}
            Current phase: <span class="text-text">{livePhaseLabel}</span>{livePercent !== null ? ` (${livePercent}%)` : ''}
          {:else}
            Backup running. This page updates when it finishes.
          {/if}
        </p>
      {/if}
    </div>

    <!-- What went wrong -->
    {#if run.error_message && run.status !== 'success' && run.status !== 'running'}
      <div class="p-4 rounded-md border {problemBoxClass[problemTone]}" role="alert">
        <div class="text-xs font-medium mb-1 uppercase tracking-wide">
          {run.status === 'failed' ? 'Backup failed' : run.status === 'partial' ? 'Completed with problems' : `Run ${run.status}`}
        </div>
        <pre class="text-sm font-mono whitespace-pre-wrap break-words">{run.error_message}</pre>
      </div>
    {/if}

    <!-- Summary -->
    <h2 class="text-sm font-medium text-text -mb-3">Results</h2>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4" data-testid="results">
      <div class="bg-bg-surface border border-border rounded-lg p-4">
        <div class="text-sm font-medium text-text mb-2">Databases</div>
        <div class="space-y-1 text-sm">
          <div class="flex justify-between"><span class="text-text-secondary">Discovered</span><span class="text-text">{run.databases_discovered ?? 0}</span></div>
          <div class="flex justify-between"><span class="text-text-secondary">Dumped</span><span class="text-text">{run.databases_dumped ?? dbCounts.success}</span></div>
          {#if (run.databases_failed ?? dbCounts.failed) > 0}
            <div class="flex justify-between"><span class="text-text-secondary">Failed</span><span class="text-danger font-medium">{run.databases_failed ?? dbCounts.failed}</span></div>
          {/if}
          {#if dbCounts.skipped > 0}
            <div class="flex justify-between"><span class="text-text-secondary">Skipped</span><span class="text-warning font-medium">{dbCounts.skipped}</span></div>
          {/if}
        </div>
      </div>

      <div class="bg-bg-surface border border-border rounded-lg p-4">
        <div class="text-sm font-medium text-text mb-2">Flash Drive</div>
        <div class="text-sm">
          {#if run.flash_backed_up}
            <span class="text-success">Archived</span>
            {#if run.flash_size_bytes}<span class="text-text-secondary"> ({formatBytes(run.flash_size_bytes)})</span>{/if}
          {:else}
            <span class="text-text-secondary">Not refreshed in this run</span>
          {/if}
        </div>
      </div>

      <div class="bg-bg-surface border border-border rounded-lg p-4">
        <div class="text-sm font-medium text-text mb-2">Storage Targets</div>
        <div class="space-y-1 text-sm">
          <div class="flex justify-between"><span class="text-text-secondary">Uploaded to</span><span class="text-text">{targetsOk} of {targets.length}</span></div>
        </div>
      </div>
    </div>

    <!-- Per-target results -->
    <div class="bg-bg-surface border border-border rounded-lg p-6">
      <h2 class="text-sm font-medium text-text mb-3">Targets</h2>
      {#if targets.length === 0}
        <p class="text-sm text-text-secondary">
          {run.status === 'running' ? 'No uploads recorded yet.' : 'No uploads were recorded for this run.'}
        </p>
      {:else}
        <div class="space-y-2">
          {#each targets as t}
            <div class="rounded-md border border-border-muted p-3">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="min-w-0">
                  <span class="text-sm font-medium text-text">{t.target_name || t.target_id}</span>
                  {#if t.target_provider}<span class="ml-2 text-xs text-text-secondary">{t.target_provider}</span>{/if}
                </div>
                <div class="flex items-center gap-3 text-xs text-text-secondary">
                  {#if t.upload_bytes}<span class="font-mono">{formatBytes(t.upload_bytes)}</span>{/if}
                  {#if t.duration_seconds}<span class="font-mono">{formatDuration(t.duration_seconds)}</span>{/if}
                  {#if t.snapshot_id}<span class="font-mono">snapshot {t.snapshot_id.slice(0, 8)}</span>{/if}
                  <StatusBadge status={t.status} size="sm" />
                </div>
              </div>
              {#if t.error}
                <p class="mt-2 text-xs font-mono whitespace-pre-wrap break-words {t.status === 'success' ? 'text-text-secondary' : 'text-danger'}">{t.error}</p>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Per-database results -->
    <div class="bg-bg-surface border border-border rounded-lg p-6">
      <h2 class="text-sm font-medium text-text mb-3">Database Dumps</h2>
      {#if databases.length === 0}
        <p class="text-sm text-text-secondary">No database dumps were recorded for this run.</p>
      {:else}
        <div class="space-y-2">
          {#each databases as d}
            <div class="rounded-md border border-border-muted p-3">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="min-w-0">
                  <span class="text-sm font-medium text-text">{d.db_name}</span>
                  <span class="ml-2 text-xs text-text-secondary">{d.container_name} · {d.db_type}</span>
                </div>
                <div class="flex items-center gap-3 text-xs text-text-secondary">
                  {#if d.dump_size_bytes}<span class="font-mono">{formatBytes(d.dump_size_bytes)}</span>{/if}
                  {#if d.integrity_check && d.status === 'success'}<span>integrity: {d.integrity_check}</span>{/if}
                  <StatusBadge status={d.status} size="sm" />
                </div>
              </div>
              {#if d.error}
                <p class="mt-2 text-xs font-mono whitespace-pre-wrap break-words {d.status === 'skipped' ? 'text-warning' : 'text-danger'}">{d.error}</p>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Log Output -->
    <div class="bg-bg-surface border border-border rounded-lg overflow-hidden">
      <div class="px-4 py-3 border-b border-border-muted">
        <h2 class="text-sm font-medium text-text">Run Logs</h2>
      </div>
      <div class="h-80">
        {#if logsLoading}
          <div class="flex items-center justify-center h-full text-text-secondary text-sm">
            Loading logs...
          </div>
        {:else}
          <LogViewer
            entries={logs}
            streaming={run.status === 'running'}
          />
        {/if}
      </div>
    </div>
  {/if}
</main>
