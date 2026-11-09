<script lang="ts">
  import { Select as SelectPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  let className: string | undefined = undefined;
  export { className as class };
</script>

<SelectPrimitive.Trigger
  class={cn(
    'flex h-9 w-full items-center justify-between rounded-md border border-border bg-bg-input px-3 py-2 text-sm text-text-primary shadow-sm',
    'placeholder:text-text-muted focus:outline-hidden focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-bg-app',
    'disabled:cursor-not-allowed disabled:opacity-50',
    className
  )}
  {...$$restProps}
>
  <slot />
  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="ml-2 opacity-50">
    <path d="m6 9 6 6 6-6" />
  </svg>
</SelectPrimitive.Trigger>
