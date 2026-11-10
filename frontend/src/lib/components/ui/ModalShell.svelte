<script context="module" lang="ts">
  let nextId = 0;

  const FOCUSABLE = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');
</script>

<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { fade } from 'svelte/transition';

  export let open: boolean = false;
  export let title: string = '';
  export let maxWidth: string = 'max-w-lg';
  export let closeOnBackdrop: boolean = true;
  export let closeOnEscape: boolean = true;
  export let danger: boolean = false;
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();
  // Unique per instance: several modals can be mounted on one page.
  const titleId = `modal-title-${++nextId}`;

  let panel: HTMLDivElement | undefined;
  let returnFocusTo: HTMLElement | null = null;
  let wasOpen = false;

  function close() {
    open = false;
    dispatch('close');
  }

  function focusableElements(): HTMLElement[] {
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
  }

  // Move focus into the dialog when it opens (the first form field, so typing
  // works straight away, else the first control) and give it back to whatever
  // opened it when it closes.
  async function handleOpenChange(isOpen: boolean) {
    if (typeof document === 'undefined') return;
    if (isOpen && !wasOpen) {
      wasOpen = true;
      returnFocusTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      await tick();
      const items = focusableElements();
      const field = items.find((el) => el.matches('input, select, textarea'));
      (field ?? items[0] ?? panel)?.focus();
    } else if (!isOpen && wasOpen) {
      wasOpen = false;
      const target = returnFocusTo;
      returnFocusTo = null;
      if (target && document.contains(target)) target.focus();
    }
  }

  $: handleOpenChange(open);

  function handleKeydown(e: KeyboardEvent) {
    if (!open) return;
    if (e.key === 'Escape' && closeOnEscape) {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === 'Tab' && panel) {
      // Keep keyboard focus inside the dialog (aria-modal promises it).
      const items = focusableElements();
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      const inside = active instanceof Node && panel.contains(active);
      if (e.shiftKey && (active === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  // Where the current press started. A drag that starts in the panel (say,
  // selecting the text typed into a confirmation field) and ends outside it
  // fires `click` on the nearest common ancestor, which is the layer, so the
  // click's own target can't tell it apart from a real backdrop click.
  let pressStartedOnLayer = false;

  function handleLayerPointerdown(e: PointerEvent) {
    pressStartedOnLayer = e.target === e.currentTarget;
  }

  function handleBackdropClick(e: MouseEvent) {
    const startedOnLayer = pressStartedOnLayer;
    pressStartedOnLayer = false;
    // Only a press that both started and ended on the backdrop area itself.
    if (closeOnBackdrop && startedOnLayer && e.target === e.currentTarget) {
      close();
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open}
  <!-- Backdrop -->
  <div
    class="fixed inset-0 z-modal-backdrop bg-black/70 backdrop-blur-xs"
    transition:fade={{ duration: 200 }}
    role="presentation"
  ></div>

  <!-- Positioning layer; clicking its empty area counts as a backdrop click. -->
  <!-- Escape is the keyboard equivalent of this click (handled on window). -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="fixed inset-0 z-modal flex items-center justify-center p-4"
    on:pointerdown={handleLayerPointerdown}
    on:click={handleBackdropClick}
    role="presentation"
  >
    <div
      bind:this={panel}
      class="w-full {maxWidth} max-h-full flex flex-col bg-bg-elevated border rounded-xl shadow-xl animate-modal-in focus:outline-hidden {danger ? 'border-danger/40' : 'border-border'} {className}"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
      aria-label={title ? undefined : 'Dialog'}
      tabindex="-1"
    >
      <!-- Header -->
      {#if title}
        <div class="flex items-center justify-between gap-3 px-6 h-[60px] shrink-0 border-b border-border-muted">
          <h2 id={titleId} class="text-base font-semibold text-text">{title}</h2>
          <button
            type="button"
            on:click={close}
            class="-mr-1.5 p-1.5 rounded text-text-secondary hover:text-text hover:bg-bg-surface transition-colors"
            aria-label="Close modal"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.5" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      {/if}

      <!-- Body -->
      <div class="px-6 py-6 max-h-[480px] min-h-0 overflow-y-auto">
        <slot />
      </div>

      <!-- Footer (optional) -->
      {#if $$slots.footer}
        <div class="flex items-center justify-end gap-3 px-6 h-[60px] shrink-0 border-t border-border-muted">
          <slot name="footer" />
        </div>
      {/if}
    </div>
  </div>
{/if}
