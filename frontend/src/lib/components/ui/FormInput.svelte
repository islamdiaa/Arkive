<script lang="ts">
  import FormLabel from './FormLabel.svelte';

  export let id: string = '';
  export let label: string = '';
  export let value: string | number = '';
  export let type: string = 'text';
  export let placeholder: string = '';
  export let required: boolean = false;
  export let disabled: boolean = false;
  export let error: string = '';
  export let mono: boolean = false;
  export let hint: string = '';
  let className: string = '';
  export { className as class };

  let touched = false;

  function handleBlur() {
    touched = true;
  }
</script>

<div class="space-y-1 {className}">
  {#if label}
    <FormLabel {label} htmlFor={id} {required} />
  {/if}

  <input
    {id}
    {type}
    bind:value
    {placeholder}
    {disabled}
    {required}
    on:blur={handleBlur}
    on:input
    on:change
    on:keydown
    aria-invalid={touched && error ? 'true' : undefined}
    aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
    class="w-full h-9 bg-bg-input border rounded-md px-3 text-sm text-text
           placeholder:text-text-secondary
           focus:border-primary focus:ring-2 focus:ring-primary/30
           disabled:opacity-50 disabled:cursor-not-allowed
           transition-colors duration-150 outline-hidden
           {label ? 'mt-1' : ''}
           {mono ? 'font-mono' : ''}
           {touched && error ? 'border-danger ring-2 ring-danger/30' : 'border-border hover:border-border-strong'}"
  />

  {#if touched && error}
    <p id="{id}-error" class="text-xs text-danger" role="alert">{error}</p>
  {:else if hint}
    <p id="{id}-hint" class="text-xs text-text-secondary">{hint}</p>
  {/if}
</div>
