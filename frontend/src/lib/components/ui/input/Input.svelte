<script lang="ts">
  import { cn } from '$lib/utils/cn';
  import type { HTMLInputAttributes } from 'svelte/elements';

  type $$Props = HTMLInputAttributes & {
    class?: string;
    value?: string;
  };

  export let value: string = '';
  export let type: HTMLInputAttributes['type'] = 'text';

  let className: string | undefined = undefined;
  export { className as class };
</script>

<input
  type={type ?? undefined}
  bind:value
  class={cn(
    'flex h-9 w-full rounded-md border border-border bg-bg-input px-3 py-1 text-sm text-text-primary shadow-sm',
    'transition-colors placeholder:text-text-muted',
    'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-bg-app',
    'disabled:cursor-not-allowed disabled:opacity-50',
    'file:border-0 file:bg-transparent file:text-sm file:font-medium',
    className
  )}
  on:input
  on:change
  on:focus
  on:blur
  on:keydown
  on:keyup
  on:paste
  {...$$restProps}
/>
