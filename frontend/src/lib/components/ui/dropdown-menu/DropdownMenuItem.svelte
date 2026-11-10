<script lang="ts">
  import { DropdownMenu as DropdownMenuPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  let className: string | undefined = undefined;
  export { className as class };

  export let disabled: boolean = false;
</script>

<DropdownMenuPrimitive.Item
  {disabled}
  class={cn(
    'relative flex cursor-pointer select-none items-center rounded-sm px-2 py-1.5 text-sm text-text-primary outline-hidden',
    'transition-colors data-highlighted:bg-bg-surface-hover data-highlighted:text-text-primary',
    'data-disabled:pointer-events-none data-disabled:opacity-50',
    className
  )}
  {...$$restProps}
>
  <slot />
</DropdownMenuPrimitive.Item>
