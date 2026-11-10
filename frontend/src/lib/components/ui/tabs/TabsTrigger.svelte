<script lang="ts">
  import { Tabs as TabsPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  export let value: string;
  export let disabled: boolean = false;

  let className: string | undefined = undefined;
  export { className as class };
</script>

<TabsPrimitive.Trigger
  {value}
  {disabled}
  class={cn(
    'inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-1 text-sm font-medium',
    'ring-offset-bg-app transition-all focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
    'disabled:pointer-events-none disabled:opacity-50',
    'data-[state=active]:bg-bg-elevated data-[state=active]:text-text-primary data-[state=active]:shadow-sm',
    className
  )}
  {...$$restProps}
>
  <slot />
</TabsPrimitive.Trigger>
