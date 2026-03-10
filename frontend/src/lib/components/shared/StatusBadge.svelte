<script lang="ts">
  export let status: string = 'unknown';
  export let size: 'sm' | 'md' = 'md';
  let className: string = '';
  export { className as class };

  const styles: Record<string, string> = {
    success: 'bg-success-bg text-success',
    completed: 'bg-success-bg text-success',
    connected: 'bg-success-bg text-success',
    backup: 'bg-primary-bg text-primary',
    restore: 'bg-info-bg text-info',
    target: 'bg-warning-bg text-warning',
    system: 'bg-neutral-bg text-neutral',
    discovery: 'bg-info-bg text-info',
    notification: 'bg-primary-bg text-primary',
    running: 'bg-info-bg text-info animate-status-pulse',
    in_progress: 'bg-info-bg text-info animate-status-pulse',
    failed: 'bg-danger-bg text-danger',
    error: 'bg-danger-bg text-danger',
    partial: 'bg-warning-bg text-warning',
    warning: 'bg-warning-bg text-warning',
    interrupted: 'bg-warning-bg text-warning',
    cancelled: 'bg-neutral-bg text-neutral',
    skipped: 'bg-neutral-bg text-neutral',
    pending: 'bg-primary-bg text-primary',
    queued: 'bg-primary-bg text-primary',
    info: 'bg-info-bg text-info',
    unknown: 'bg-neutral-bg text-neutral',
  };

  const icons: Record<string, string> = {
    success: 'ok', completed: 'ok', connected: 'ok',
    backup: 'B', restore: 'R', target: 'T', system: 'S', discovery: 'D', notification: 'N',
    running: 'o', in_progress: 'o',
    failed: 'x', error: 'x',
    partial: '~', warning: '!', interrupted: '!',
    cancelled: '-', skipped: '-',
    pending: 'o', queued: 'o',
    info: 'i', unknown: '?',
  };

  $: resolvedStyle = styles[status] || styles.unknown;
  $: resolvedIcon = icons[status] || '?';
  $: sizeClasses = size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs';
</script>

<span class="inline-flex items-center gap-1 rounded-full font-medium {sizeClasses} {resolvedStyle} {className}">
  <span class="text-[10px] leading-none">{resolvedIcon}</span>
  {status}
</span>
