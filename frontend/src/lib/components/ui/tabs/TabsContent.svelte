<script lang="ts">
  import { Tabs as TabsPrimitive } from 'bits-ui';
  import { cn } from '$lib/utils/cn';

  export let value: string;

  let className: string | undefined = undefined;
  export { className as class };
</script>

<TabsPrimitive.Content
  {value}
  class={cn(
    'mt-2 ring-offset-bg-app focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
    className
  )}
  {...$$restProps}
>
  <slot />
</TabsPrimitive.Content>
