<script lang="ts">
  import { DropdownMenu as DropdownMenuPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  let className: string | undefined = undefined;
  export { className as class };
</script>

<DropdownMenuPrimitive.Trigger class={cn('outline-hidden', className)} {...$$restProps}>
  <slot />
</DropdownMenuPrimitive.Trigger>
