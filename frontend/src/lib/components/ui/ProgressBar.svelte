<script lang="ts">
  export let value: number = 0;
  export let max: number = 100;
  export let active: boolean = false;
  export let variant: 'primary' | 'success' | 'warning' | 'danger' | 'info' = 'primary';
  export let size: 'sm' | 'md' | 'lg' = 'md';
  export let showLabel: boolean = false;
  export let indeterminate: boolean = false;
  let className: string = '';
  export { className as class };

  $: percent = Math.min(100, Math.max(0, (value / max) * 100));

  const variantMap: Record<string, string> = {
    primary: 'bg-primary',
    success: 'bg-success',
    warning: 'bg-warning',
    danger: 'bg-danger',
    info: 'bg-info',
  };

  const sizeMap: Record<string, string> = {
    sm: 'h-1',
    md: 'h-1.5',
    lg: 'h-2.5',
  };

  $: heightClass = sizeMap[size] ?? 'h-1.5';
  $: colorClass = variantMap[variant] ?? 'bg-primary';
  $: isAnimated = active || indeterminate;
</script>

<div class="w-full {className}" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
  {#if showLabel}
    <div class="flex justify-between mb-1">
      <slot name="label" />
      <span class="text-xs font-mono text-text-secondary">{Math.round(percent)}%</span>
    </div>
  {/if}

  <div class="{heightClass} bg-bg-elevated rounded-full overflow-hidden">
    <div
      class="{heightClass} rounded-full transition-all duration-300 ease-out
             {isAnimated ? 'animate-progress-shimmer bg-linear-to-r from-primary via-info to-primary' : colorClass}
             {isAnimated ? 'shadow-[0_0_8px_rgba(56,139,253,0.4)]' : ''}"
      style="width: {indeterminate ? '100' : percent}%"
    ></div>
  </div>
</div>
