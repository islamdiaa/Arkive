<script lang="ts">
  import { DropdownMenu as DropdownMenuPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  let className: string | undefined = undefined;
  export { className as class };

  export let align: 'start' | 'center' | 'end' = 'center';
  export let sideOffset: number = 4;
  export let side: 'top' | 'bottom' | 'left' | 'right' = 'bottom';
</script>

<DropdownMenuPrimitive.Content
  {align}
  {sideOffset}
  {side}
  class={cn(
    'z-dropdown min-w-32 overflow-hidden rounded-md border border-border bg-bg-elevated p-1 shadow-lg',
    'focus:outline-hidden',
    className
  )}
  {...$$restProps}
>
  <slot />
</DropdownMenuPrimitive.Content>
