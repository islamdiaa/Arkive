<script lang="ts">
  import { Select as SelectPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  let className: string | undefined = undefined;
  export { className as class };

  export let sideOffset: number = 4;
  export let side: 'top' | 'bottom' = 'bottom';
</script>

<SelectPrimitive.Content
  {sideOffset}
  {side}
  class={cn(
    'relative z-dropdown min-w-32 overflow-hidden rounded-md border border-border bg-bg-elevated shadow-lg',
    'focus:outline-hidden',
    className
  )}
  {...$$restProps}
>
  <div class="p-1">
    <slot />
  </div>
</SelectPrimitive.Content>
