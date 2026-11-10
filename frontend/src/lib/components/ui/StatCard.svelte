<script lang="ts">
  export let label: string;
  export let value: string;
  export let delta: number | null = null;
  export let gradient: string = 'from-(--color-primary) to-info';
  export let mono: boolean = true;
  let className: string = '';
  export { className as class };
</script>

<div class="card relative overflow-hidden {className}">
  <!-- 2px gradient top stripe -->
  <div class="absolute top-0 inset-x-0 h-0.5 bg-linear-to-r {gradient}"></div>

  <p class="text-xs uppercase tracking-wider text-text-secondary mb-2">{label}</p>
  <p class="{mono ? 'font-mono' : ''} text-[28px] font-semibold text-text">{value}</p>

  {#if delta !== null}
    <p class="text-xs mt-1 {delta > 0 ? 'text-success' : delta < 0 ? 'text-danger' : 'text-text-secondary'}">
      {#if delta > 0}↑{:else if delta < 0}↓{/if}
      {Math.abs(delta)}%
    </p>
  {/if}

  <slot />
</div>
