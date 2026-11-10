<script lang="ts">
  import { cn } from '$lib/utils/cn';
  import type { HTMLButtonAttributes } from 'svelte/elements';

  type Variant = 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link';
  type Size = 'default' | 'sm' | 'lg' | 'icon';

  interface $$Props extends HTMLButtonAttributes {
    variant?: Variant;
    size?: Size;
    class?: string;
    href?: string;
    disabled?: boolean;
  }

  export let variant: Variant = 'default';
  export let size: Size = 'default';
  export let href: string | undefined = undefined;
  export let disabled: boolean = false;

  let className: string | undefined = undefined;
  export { className as class };

  const variantClasses: Record<Variant, string> = {
    default: 'bg-primary text-text-on-primary hover:bg-primary-hover shadow-sm',
    destructive: 'bg-error text-text-on-primary hover:bg-error/90 shadow-sm',
    outline: 'border border-border bg-transparent hover:bg-bg-surface-hover text-text-primary',
    secondary: 'bg-bg-elevated text-text-primary hover:bg-bg-surface-hover',
    ghost: 'hover:bg-bg-surface-hover text-text-primary',
    link: 'text-text-link underline-offset-4 hover:underline',
  };

  const sizeClasses: Record<Size, string> = {
    default: 'h-9 px-4 py-2',
    sm: 'h-8 px-3 text-xs',
    lg: 'h-10 px-6 text-base',
    icon: 'h-9 w-9',
  };

  $: baseClasses = cn(
    'inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium',
    'transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-bg-app',
    'disabled:pointer-events-none disabled:opacity-50',
    variantClasses[variant],
    sizeClasses[size],
    className
  );
</script>

{#if href}
  <a {href} class={baseClasses} {...$$restProps}>
    <slot />
  </a>
{:else}
  <button {disabled} class={baseClasses} on:click on:focus on:blur on:keydown on:keyup on:mouseenter on:mouseleave {...$$restProps}>
    <slot />
  </button>
{/if}
