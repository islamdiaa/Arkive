<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let tabs: { id: string; label: string; count?: number }[] = [];
  export let activeTab: string = '';
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();

  function selectTab(id: string) {
    activeTab = id;
    dispatch('change', { tab: id });
  }

  function handleKeydown(e: KeyboardEvent) {
    const idx = tabs.findIndex(t => t.id === activeTab);
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      const next = (idx + 1) % tabs.length;
      selectTab(tabs[next].id);
      (e.currentTarget as HTMLElement)?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = (idx - 1 + tabs.length) % tabs.length;
      selectTab(tabs[prev].id);
      (e.currentTarget as HTMLElement)?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[prev]?.focus();
    }
  }
</script>

<!-- svelte-ignore a11y-interactive-supports-focus -->
<div class="flex gap-1 p-1 bg-bg-surface rounded-lg {className}" role="tablist" on:keydown={handleKeydown}>
  {#each tabs as tab}
    <button
      role="tab"
      aria-selected={activeTab === tab.id}
      tabindex={activeTab === tab.id ? 0 : -1}
      class="px-4 py-1.5 rounded-md text-sm font-medium transition-colors duration-120
             {activeTab === tab.id
               ? 'bg-bg-elevated text-text shadow-sm'
               : 'text-text-secondary hover:text-text'}"
      on:click={() => selectTab(tab.id)}
    >
      {tab.label}
      {#if tab.count !== undefined}
        <span class="ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] bg-bg-elevated text-text-secondary min-w-[20px] inline-flex items-center justify-center">
          {tab.count}
        </span>
      {/if}
    </button>
  {/each}
</div>
