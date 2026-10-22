<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
  import { backupApi } from '$lib/api/backup';
  import { subscribe } from '$lib/stores/sse';
  import { backupRunning } from '$lib/stores/app';
  import { BACKUP_PHASES, phasesForJobType, phaseIndexIn } from '$lib/constants/backup-phases';
  import {
    isConflictTerminalEvent,
    panelStatusForCancellation,
    panelStatusForCompletion,
    shouldIgnoreTerminalEvent,
    type PanelStatus,
  } from '$lib/utils/run-status';
  import { jobTypeLabel } from '$lib/utils/job-types';

  const STORAGE_KEY = 'arkive_backup_progress';
  const AUTO_HIDE_MS = 10000;

  interface PersistedProgressState {
    runId: string | null;
    visible: boolean;
    /** Legacy field; superseded by currentPhaseKey. */
    currentPhaseIndex?: number;
    currentPhaseKey?: string | null;
    percent: number;
    status: PanelStatus;
    errorMessage: string;
    userAction?: string;
    jobType?: string | null;
    updatedAt: string;
  }

  let visible = false;
  let currentPhaseKey: string | null = null;
  let percent = 0;
  let status: PanelStatus = 'idle';
  let errorMessage = '';
  let userAction = '';
  let runId: string | null = null;
  let jobType: string | null = null;
  let hideTimer: ReturnType<typeof setTimeout> | null = null;

  $: phases = phasesForJobType(jobType);
  $: phaseLabels = phases.map(p => p.label);
  $: currentPhaseIndex = status === 'success' ? phases.length : phaseIndexIn(phases, currentPhaseKey);
  $: isTerminal = status !== 'idle' && status !== 'running';

  const badge: Record<string, { label: string; cls: string }> = {
    running: { label: 'Running', cls: 'bg-primary/20 text-primary' },
    success: { label: 'Complete', cls: 'bg-success/20 text-success' },
    partial: { label: 'Completed with problems', cls: 'bg-warning/20 text-warning' },
    failed: { label: 'Failed', cls: 'bg-danger/20 text-danger' },
    cancelled: { label: 'Cancelled', cls: 'bg-neutral-bg text-neutral' },
    skipped: { label: 'Skipped', cls: 'bg-neutral-bg text-neutral' },
  };

  const progressVariant: Record<string, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
    running: 'primary',
    success: 'success',
    partial: 'warning',
    failed: 'danger',
    cancelled: 'info',
    skipped: 'info',
  };

  function currentNodeClass(s: PanelStatus): string {
    switch (s) {
      case 'running': return 'bg-info text-white animate-pulse';
      case 'failed': return 'bg-danger text-white';
      case 'partial': return 'bg-warning text-white';
      case 'cancelled':
      case 'skipped': return 'bg-neutral text-white';
      default: return 'border-2 border-border-muted bg-transparent text-text-tertiary';
    }
  }

  function clearHideTimer() {
    if (hideTimer !== null) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function persistState() {
    if (typeof sessionStorage === 'undefined') return;
    if (status === 'idle' || (!visible && status !== 'running')) {
      sessionStorage.removeItem(STORAGE_KEY);
      return;
    }
    const snapshot: PersistedProgressState = {
      runId,
      visible,
      currentPhaseKey,
      percent,
      status,
      errorMessage,
      userAction,
      jobType,
      updatedAt: new Date().toISOString(),
    };
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
  }

  function clearPersistedState() {
    if (typeof sessionStorage === 'undefined') return;
    sessionStorage.removeItem(STORAGE_KEY);
  }

  function restorePersistedState(): PersistedProgressState | null {
    if (typeof sessionStorage === 'undefined') return null;
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as PersistedProgressState;
    } catch {
      sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
  }

  function applyState(snapshot: PersistedProgressState) {
    runId = snapshot.runId;
    visible = snapshot.visible;
    currentPhaseKey =
      snapshot.currentPhaseKey ??
      (typeof snapshot.currentPhaseIndex === 'number' ? BACKUP_PHASES[snapshot.currentPhaseIndex]?.key ?? null : null);
    percent = snapshot.percent;
    status = snapshot.status;
    errorMessage = snapshot.errorMessage ?? '';
    userAction = snapshot.userAction ?? '';
    jobType = snapshot.jobType ?? null;
  }

  function resetToIdle() {
    clearHideTimer();
    visible = false;
    status = 'idle';
    runId = null;
    jobType = null;
    currentPhaseKey = null;
    percent = 0;
    errorMessage = '';
    userAction = '';
    clearPersistedState();
  }

  function dismiss() {
    resetToIdle();
  }

  /** Best-effort fetch of job type (for the phase rail) and error summary. */
  async function loadRunDetails(id: string, wantError: boolean) {
    try {
      const run: any = await backupApi.getRun(id);
      if (!run || id !== runId) return;
      if (run.job_type) jobType = run.job_type;
      if (wantError && run.error_message && (status === 'partial' || status === 'failed')) {
        errorMessage = run.error_message;
      }
      persistState();
    } catch {
      // Details are optional; the panel works without them.
    }
  }

  async function hydrateRunningState() {
    const persisted = restorePersistedState();
    if (persisted) {
      applyState(persisted);
    }

    try {
      const response = await backupApi.listRuns({ status: 'running', limit: 1 });
      const activeRun: any = response.items?.[0] ?? null;
      if (activeRun) {
        backupRunning.set(true);
        clearHideTimer();
        visible = true;
        status = 'running';
        if (persisted?.runId !== activeRun.id) {
          runId = activeRun.id;
          currentPhaseKey = null;
          percent = 0;
          errorMessage = '';
          userAction = '';
          jobType = null;
        }
        if (activeRun.job_type) jobType = activeRun.job_type;
        persistState();
        return;
      }
    } catch {
      // Ignore bootstrap failures and wait for SSE updates.
    }

    backupRunning.set(false);
    if (status === 'running') {
      resetToIdle();
    }
  }

  function startAutoHide() {
    clearHideTimer();
    hideTimer = setTimeout(() => {
      resetToIdle();
    }, AUTO_HIDE_MS);
  }

  const unsubStarted = subscribe('backup:started', (event) => {
    const data = event.data as { run_id?: string };
    clearHideTimer();
    const newRun = (data.run_id ?? null) !== runId;
    visible = true;
    status = 'running';
    runId = data.run_id ?? null;
    currentPhaseKey = null;
    percent = 0;
    errorMessage = '';
    userAction = '';
    if (newRun) jobType = null;
    backupRunning.set(true);
    persistState();
    if (runId) loadRunDetails(runId, false);
  });

  const unsubProgress = subscribe('backup:progress', (event) => {
    // Progress is only emitted by the run holding the backup lock, so it
    // always describes the real active run.
    const data = event.data as { run_id?: string; phase?: string; percent?: number };
    const switched = !!data.run_id && data.run_id !== runId;
    if (switched) {
      runId = data.run_id ?? null;
      jobType = null;
      errorMessage = '';
      userAction = '';
    }
    clearHideTimer();
    if (data.phase) {
      currentPhaseKey = data.phase;
    }
    if (typeof data.percent === 'number') {
      percent = data.percent;
    }
    status = 'running';
    visible = true;
    backupRunning.set(true);
    persistState();
    if (switched && runId) loadRunDetails(runId, false);
  });

  function ignoreTerminal(data: Record<string, unknown>): boolean {
    if (shouldIgnoreTerminalEvent({ runId, status }, data.run_id as string | undefined)) return true;
    // A conflict event for an unknown run must not end a run we are showing.
    if (status === 'running' && isConflictTerminalEvent(data)) return true;
    return false;
  }

  const unsubCompleted = subscribe('backup:completed', (event) => {
    const data = event.data as { run_id?: string; status?: string };
    if (ignoreTerminal(data)) return;
    runId = data.run_id ?? runId;
    status = panelStatusForCompletion(data.status);
    visible = true;
    userAction = '';
    backupRunning.set(false);
    clearHideTimer();
    if (status === 'success') {
      errorMessage = '';
      percent = 100;
      startAutoHide();
    } else {
      errorMessage = status === 'failed'
        ? 'Backup failed: no data reached any storage target.'
        : 'Backup finished, but some steps failed.';
      if (runId) loadRunDetails(runId, true);
    }
    persistState();
  });

  const unsubFailed = subscribe('backup:failed', (event) => {
    const data = event.data as { run_id?: string; error?: string; message?: string; user_action?: string };
    if (ignoreTerminal(data)) return;
    runId = data.run_id ?? runId;
    status = 'failed';
    errorMessage = data.error || data.message || 'Backup failed';
    userAction = data.user_action || '';
    visible = true;
    backupRunning.set(false);
    clearHideTimer();
    persistState();
  });

  const unsubCancelled = subscribe('backup:cancelled', (event) => {
    const data = event.data as { run_id?: string; status?: string; error?: string; message?: string };
    if (ignoreTerminal(data)) return;
    runId = data.run_id ?? runId;
    status = panelStatusForCancellation(data);
    errorMessage = status === 'skipped'
      ? data.error || data.message || 'A run was skipped because another backup was already in progress.'
      : data.message || 'Backup was cancelled.';
    userAction = '';
    visible = true;
    backupRunning.set(false);
    clearHideTimer();
    persistState();
  });

  onMount(() => {
    hydrateRunningState();
  });

  onDestroy(() => {
    unsubStarted();
    unsubProgress();
    unsubCompleted();
    unsubFailed();
    unsubCancelled();
    clearHideTimer();
  });
</script>

{#if visible || $backupRunning}
  <div class="bg-bg-surface border border-border rounded-lg p-5 space-y-4" data-testid="backup-progress-panel" data-status={status}>
    <div class="flex items-center justify-between gap-3">
      <h3 class="font-semibold text-text text-sm">
        Backup Progress
        {#if jobType}
          <span class="ml-2 text-xs font-normal text-text-secondary">{jobTypeLabel(jobType)}</span>
        {/if}
      </h3>
      <div class="flex items-center gap-2">
        {#if badge[status]}
          <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium {badge[status].cls}">
            {#if status === 'running'}
              <span class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>
            {/if}
            {badge[status].label}
          </span>
        {/if}
        {#if isTerminal}
          <button
            on:click={dismiss}
            class="text-text-secondary hover:text-text text-xs px-1"
            aria-label="Dismiss backup progress"
          >✕</button>
        {/if}
      </div>
    </div>

    <!-- Horizontal phase rail -->
    <div class="flex items-center">
      {#each phaseLabels as phase, i}
        <div class="flex items-center" style="flex: {i < phaseLabels.length - 1 ? '1' : '0 0 auto'}">
          <!-- Node -->
          <div class="flex flex-col items-center gap-1 shrink-0">
            <div
              class="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-semibold transition-all duration-300
                {i < currentPhaseIndex
                  ? 'bg-success text-white'
                  : i === currentPhaseIndex
                    ? currentNodeClass(status)
                    : 'border-2 border-border-muted bg-transparent text-text-tertiary'}"
            >
              {#if i < currentPhaseIndex}
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7" />
                </svg>
              {:else}
                {i + 1}
              {/if}
            </div>
            <span class="text-[10px] text-text-tertiary whitespace-nowrap leading-tight max-w-[52px] text-center">{phase}</span>
          </div>
          <!-- Connector line (not after last node) -->
          {#if i < phaseLabels.length - 1}
            <div
              class="flex-1 h-0.5 mx-1 mb-4 transition-colors duration-300
                {i < currentPhaseIndex ? 'bg-success' : 'bg-border-muted'}"
            ></div>
          {/if}
        </div>
      {/each}
    </div>

    <ProgressBar
      value={percent}
      max={100}
      size="sm"
      variant={progressVariant[status] ?? 'primary'}
      active={status === 'running'}
    />

    {#if status === 'success'}
      <p class="text-xs text-text-secondary">Backup completed successfully. This panel will close in 10 seconds.</p>
    {:else if status === 'partial' || status === 'failed'}
      <div class="space-y-1">
        <p class="text-xs {status === 'failed' ? 'text-danger' : 'text-warning'}">{errorMessage}</p>
        {#if userAction}
          <p class="text-xs text-text-secondary">{userAction}</p>
        {/if}
        {#if runId}
          <a href="/backups/{runId}" class="text-xs text-primary hover:text-primary-hover">View run details</a>
        {/if}
      </div>
    {:else if status === 'cancelled' || status === 'skipped'}
      <p class="text-xs text-text-secondary">{errorMessage}</p>
    {/if}
  </div>
{/if}
