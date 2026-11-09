<script lang="ts">
  import { cn } from '$lib/utils/cn';

  type Variant = 'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning';

  export let variant: Variant = 'default';

  let className: string | undefined = undefined;
  export { className as class };

  const variantClasses: Record<Variant, string> = {
    default: 'border-transparent bg-primary text-text-on-primary',
    secondary: 'border-transparent bg-bg-elevated text-text-secondary',
    destructive: 'border-transparent bg-error text-text-on-primary',
    outline: 'border border-border text-text-primary bg-transparent',
    success: 'border-transparent bg-success-bg text-success',
    warning: 'border-transparent bg-warning-bg text-warning',
  };
</script>

<span
  class={cn(
    'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors',
    'focus:outline-hidden focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-bg-app',
    variantClasses[variant],
    className
  )}
  {...$$restProps}
>
  <slot />
</span>
