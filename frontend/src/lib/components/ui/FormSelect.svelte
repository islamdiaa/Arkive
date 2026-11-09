<script lang="ts">
  import FormLabel from './FormLabel.svelte';

  export let id: string = '';
  export let label: string = '';
  export let value: string = '';
  export let options: { value: string; label: string }[] = [];
  export let required: boolean = false;
  export let disabled: boolean = false;
  export let error: string = '';
  export let placeholder: string = '';
  let className: string = '';
  export { className as class };
</script>

<div class="space-y-1 {className}">
  {#if label}
    <FormLabel {label} htmlFor={id} {required} />
  {/if}

  <select
    {id}
    bind:value
    {disabled}
    {required}
    on:change
    aria-invalid={error ? 'true' : undefined}
    aria-describedby={error ? `${id}-error` : undefined}
    class="w-full h-9 bg-bg-input border rounded-md px-3 text-sm text-text
           focus:border-primary focus:ring-2 focus:ring-primary/30
           disabled:opacity-50 disabled:cursor-not-allowed
           transition-colors duration-150 outline-hidden appearance-none
           {label ? 'mt-1' : ''}
           {error ? 'border-danger ring-2 ring-danger/30' : 'border-border hover:border-border-strong'}"
  >
    {#if placeholder}
      <option value="" disabled>{placeholder}</option>
    {/if}
    {#each options as opt}
      <option value={opt.value}>{opt.label}</option>
    {/each}
  </select>

  {#if error}
    <p id="{id}-error" class="text-xs text-danger" role="alert">{error}</p>
  {/if}
</div>
