<script lang="ts">
  import { Switch as SwitchPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  export let checked: boolean = false;
  export let disabled: boolean = false;

  let className: string | undefined = undefined;
  export { className as class };
</script>

<SwitchPrimitive.Root
  bind:checked
  {disabled}
  class={cn(
    'peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent shadow-sm',
    'transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-bg-app',
    'disabled:cursor-not-allowed disabled:opacity-50',
    'data-[state=checked]:bg-primary data-[state=unchecked]:bg-bg-elevated',
    className
  )}
  {...$$restProps}
>
  <SwitchPrimitive.Thumb
    class={cn(
      'pointer-events-none block h-4 w-4 rounded-full bg-text-on-primary shadow-sm ring-0 transition-transform',
      'data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0'
    )}
  />
</SwitchPrimitive.Root>
