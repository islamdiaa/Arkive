<script lang="ts">
  import { Search } from 'lucide-svelte';
  import { cn } from '$lib/utils/cn';
  import { createEventDispatcher } from 'svelte';

  export let searchPlaceholder: string = 'Search...';
  export let searchValue: string = '';
  export let filters: { key: string; label: string; options: { value: string; label: string }[] }[] = [];
  export let filterValues: Record<string, string> = {};
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();

  function handleSearch(e: Event) {
    searchValue = (e.target as HTMLInputElement).value;
    dispatch('change', { searchValue, filterValues });
  }

  function handleFilter(key: string, e: Event) {
    filterValues[key] = (e.target as HTMLSelectElement).value;
    filterValues = { ...filterValues };
    dispatch('change', { searchValue, filterValues });
  }

  $: activeFilterCount = Object.values(filterValues).filter(Boolean).length;
</script>

<div class={cn('flex flex-wrap items-center gap-3 mb-4', className)}>
  <div class="relative flex-1 min-w-[200px] max-w-sm">
    <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary" strokeWidth={1.5} />
    <input
      type="text"
      value={searchValue}
      on:input={handleSearch}
      placeholder={searchPlaceholder}
      aria-label={searchPlaceholder}
      class="w-full h-9 bg-bg-input border border-border rounded-md pl-9 pr-3 text-sm text-text
             placeholder:text-text-secondary focus:border-primary focus:ring-2 focus:ring-primary/30
             outline-hidden transition-colors"
    />
  </div>

  {#each filters as filter}
    <div class="relative">
      <select
        value={filterValues[filter.key] || ''}
        on:change={(e) => handleFilter(filter.key, e)}
        aria-label="Filter by {filter.label}"
        class="h-9 bg-bg-input border border-border rounded-md px-3 pr-8 text-sm text-text
               focus:border-primary focus:ring-2 focus:ring-primary/30
               outline-hidden transition-colors appearance-none cursor-pointer"
      >
        <option value="">{filter.label}: All</option>
        {#each filter.options as opt}
          <option value={opt.value}>{opt.label}</option>
        {/each}
      </select>
      <svg class="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary pointer-events-none"
           fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.5">
        <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
      </svg>
    </div>
  {/each}

  {#if activeFilterCount > 0}
    <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-primary-bg text-primary font-medium">
      {activeFilterCount} filter{activeFilterCount !== 1 ? 's' : ''} active
    </span>
  {/if}

  <slot />
</div>
