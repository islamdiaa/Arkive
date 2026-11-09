<script lang="ts">
  import { cn } from '$lib/utils/cn';

  export let placeholder: string = 'Select an option';

  let className: string | undefined = undefined;
  export { className as class };
</script>

<span
  class={cn('pointer-events-none text-text-primary data-placeholder:text-text-muted', className)}
  {...$$restProps}
>
  <slot>{placeholder}</slot>
</span>
