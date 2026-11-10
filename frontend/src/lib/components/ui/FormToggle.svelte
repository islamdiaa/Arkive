<script context="module" lang="ts">
  let nextId = 0;
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let id: string = '';
  export let label: string = '';
  /** Accessible name when there is no visible label (e.g. a toggle in a list row). */
  export let ariaLabel: string = '';
  export let checked: boolean = false;
  export let disabled: boolean = false;
  export let description: string = '';
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();
  // A <label for=""> labels nothing, so fall back to a generated id.
  const fallbackId = `form-toggle-${++nextId}`;
  $: switchId = id || fallbackId;
  $: descriptionId = description ? `${switchId}-description` : undefined;

  function toggle() {
    if (!disabled) {
      dispatch('change', { checked: !checked });
    }
  }
</script>

<div class="flex items-center justify-between gap-3 {className}">
  <div>
    {#if label}
      <label for={switchId} class="text-sm font-medium text-text cursor-pointer">{label}</label>
    {/if}
    {#if description}
      <p id={descriptionId} class="text-xs text-text-secondary mt-0.5">{description}</p>
    {/if}
  </div>
  <!-- The visible track stays 20px tall; the 24px-tall button around it is
       the tap target. -->
  <button
    id={switchId}
    type="button"
    role="switch"
    aria-checked={checked}
    aria-label={ariaLabel || label || undefined}
    aria-describedby={descriptionId}
    {disabled}
    on:click={toggle}
    on:click
    class="group relative inline-flex h-6 w-10 shrink-0 items-center justify-center rounded-full
           focus:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-bg-app
           {disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}"
  >
    <span
      class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-150
             {checked ? 'bg-primary' : 'bg-border-strong'}"
      aria-hidden="true"
    >
      <span
        class="inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-150
               {checked ? 'translate-x-4' : 'translate-x-0.5'}"
      ></span>
    </span>
  </button>
</div>
