<script lang="ts">
  import { onMount } from 'svelte';

  export let entries: Array<{ timestamp: string; level: string; component: string; message: string }> = [];
  export let streaming: boolean = false;
  export let filterLevel: string = 'ALL';

  let container: HTMLDivElement | undefined;
  let autoScroll = true;

  const levelBorderClass: Record<string, string> = {
    ERROR: 'border-l-danger',
    WARN: 'border-l-warning',
  };

  const levelTextClass: Record<string, string> = {
    DEBUG: 'text-text-tertiary',
    INFO: 'text-primary',
    WARN: 'text-warning',
    ERROR: 'text-danger',
  };

  $: filteredEntries = filterLevel === 'ALL'
    ? entries
    : entries.filter(e => e.level === filterLevel);

  $: if (filteredEntries.length) {
    scrollToBottom();
  }

  function scrollToBottom() {
    if (container && autoScroll) {
      container.scrollTop = container.scrollHeight;
    }
  }
</script>

<div class="flex flex-col h-full">
  <div class="flex items-center gap-2 px-4 py-2 border-b border-border">
    <select
      bind:value={filterLevel}
      class="bg-bg-input border border-border rounded px-2 py-1 text-xs text-text focus:border-primary outline-hidden"
    >
      <option value="ALL">All Levels</option>
      <option value="DEBUG">Debug</option>
      <option value="INFO">Info</option>
      <option value="WARN">Warning</option>
      <option value="ERROR">Error</option>
    </select>
    {#if streaming}
      <span class="flex items-center gap-1 text-xs text-success">
        <span class="w-1.5 h-1.5 rounded-full bg-success animate-pulse"></span>
        Live
      </span>
    {/if}
    <label class="ml-auto flex items-center gap-1 text-xs text-text-secondary cursor-pointer">
      <input type="checkbox" bind:checked={autoScroll} class="rounded" />
      Auto-scroll
    </label>
  </div>

  <div bind:this={container} class="flex-1 overflow-y-auto bg-bg-base">
    {#each filteredEntries as entry, i}
      {@const borderClass = levelBorderClass[entry.level] || ''}
      <div class="flex gap-3 px-4 py-0.5 hover:bg-bg-surface/40 border-b border-border/10 border-l-[3px] {borderClass || 'border-l-transparent'} transition-colors">
        <span class="font-mono text-[13px] text-text-tertiary text-right w-[3ch] shrink-0 select-none">{i + 1}</span>
        <span class="font-mono text-[13px] text-text-secondary whitespace-nowrap w-40 shrink-0 truncate">{entry.timestamp}</span>
        <span class="font-mono text-[13px] w-12 text-right shrink-0 {levelTextClass[entry.level] || 'text-text-secondary'}">{entry.level}</span>
        <span class="font-mono text-[13px] text-text-secondary shrink-0">[{entry.component}]</span>
        <span class="font-mono text-[13px] text-text flex-1 break-all">{entry.message}</span>
      </div>
    {:else}
      <div class="font-mono text-[13px] text-center text-text-secondary py-8">No log entries</div>
    {/each}
  </div>
</div>
