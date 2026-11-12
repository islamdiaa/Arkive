<script lang="ts">
  import { formatBytes, formatDuration } from '$lib/utils/format';
  import type { BackupTransfer } from '$lib/types';

  export let transfer: BackupTransfer;
  export let target = '';

  $: percent = transfer.total_bytes && transfer.percent_done != null
    ? Math.max(0, Math.min(100, Math.round(transfer.percent_done * 100))) : null;
  $: speed = transfer.seconds_elapsed && transfer.bytes_done
    ? transfer.bytes_done / transfer.seconds_elapsed : null;
</script>

<div class="space-y-3 text-sm" data-testid="backup-transfer">
  {#if target}<p class="font-medium text-text">Target: {target}</p>{/if}
  <div class="h-2 rounded bg-bg-surface-hover overflow-hidden" role="progressbar" aria-label="Backup data processed" aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent ?? undefined}>
    <div class="h-full bg-primary transition-all" style:width={percent === null ? '0%' : `${percent}%`}></div>
  </div>
  <dl class="grid grid-cols-2 sm:grid-cols-4 gap-3">
    <div>
      <dt class="text-xs text-text-secondary">Processed</dt>
      <dd class="text-text">{formatBytes(transfer.bytes_done ?? 0)} / {transfer.total_bytes ? formatBytes(transfer.total_bytes) : 'Scanning…'}{percent !== null ? ` (${percent}%)` : ''}</dd>
    </div>
    <div>
      <dt class="text-xs text-text-secondary">Files</dt>
      <dd class="text-text">{transfer.files_done ?? 0} / {transfer.total_files ?? '—'}</dd>
    </div>
    <div>
      <dt class="text-xs text-text-secondary">Processing speed (average)</dt>
      <dd class="text-text">{speed ? `${formatBytes(Math.round(speed))}/s` : 'Calculating…'}</dd>
    </div>
    <div>
      <dt class="text-xs text-text-secondary">Estimated time remaining</dt>
      <dd class="text-text">{transfer.seconds_remaining != null ? `~${formatDuration(Math.ceil(transfer.seconds_remaining))}` : 'Calculating…'}</dd>
    </div>
  </dl>
  <p class="text-xs text-text-secondary">Source data processed, before deduplication and compression. Uploaded size and file changes are available per target when its upload finishes. The estimate covers this target only.</p>
</div>
