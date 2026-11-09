<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let id: string = '';
  export let label: string = '';
  export let checked: boolean = false;
  export let disabled: boolean = false;
  export let description: string = '';
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();

  function toggle() {
    if (!disabled) {
      dispatch('change', { checked: !checked });
    }
  }
</script>

<div class="flex items-center justify-between {className}">
  <div>
    {#if label}
      <label for={id} class="text-sm font-medium text-text cursor-pointer">{label}</label>
    {/if}
    {#if description}
      <p class="text-xs text-text-secondary mt-0.5">{description}</p>
    {/if}
  </div>
  <button
    {id}
    type="button"
    role="switch"
    aria-checked={checked}
    aria-label={label || undefined}
    {disabled}
    on:click={toggle}
    on:click
    class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-150
           focus:outline-hidden focus:ring-2 focus:ring-primary/30
           {disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
           {checked ? 'bg-primary' : 'bg-border-strong'}"
  >
    <span
      class="inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform duration-150
             {checked ? 'translate-x-4' : 'translate-x-0.5'}"
    ></span>
  </button>
</div>
