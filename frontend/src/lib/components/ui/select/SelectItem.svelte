<script lang="ts">
  import { Select as SelectPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  export let value: string;
  export let label: string | undefined = undefined;
  export let disabled: boolean = false;

  let className: string | undefined = undefined;
  export { className as class };
</script>

<SelectPrimitive.Item
  {value}
  label={label ?? value}
  {disabled}
  class={cn(
    'relative flex w-full cursor-pointer select-none items-center rounded-sm py-1.5 pl-8 pr-2 text-sm text-text-primary outline-hidden',
    'data-highlighted:bg-bg-surface-hover data-highlighted:text-text-primary',
    'data-disabled:pointer-events-none data-disabled:opacity-50',
    className
  )}
  {...$$restProps}
>
  <slot>
    {label ?? value}
  </slot>
</SelectPrimitive.Item>
