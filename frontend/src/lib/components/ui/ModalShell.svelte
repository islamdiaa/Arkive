<script lang="ts">
  import { createEventDispatcher } from 'svelte';
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

  function close() {
    open = false;
    dispatch('close');
  }

  function handleKeydown(e: KeyboardEvent) {
    if (!open) return;
    if (e.key === 'Escape' && closeOnEscape) {
      close();
    }
  }

  function handleBackdropClick() {
    if (closeOnBackdrop) {
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
    on:click={handleBackdropClick}
    on:keydown={() => {}}
    role="presentation"
  ></div>

  <!-- Modal -->
  <div
    class="fixed inset-0 z-modal flex items-center justify-center p-4"
    role="dialog"
    aria-modal="true"
    aria-labelledby="modal-title"
  >
    <div
      class="w-full {maxWidth} bg-bg-elevated border rounded-xl shadow-xl animate-modal-in {danger ? 'border-danger/40' : 'border-border'} {className}"
      on:click|stopPropagation={() => {}}
      on:keydown|stopPropagation={() => {}}
      role="presentation"
    >
      <!-- Header -->
      {#if title}
        <div class="flex items-center justify-between px-6 h-[60px] border-b border-border-muted">
          <h2 id="modal-title" class="text-base font-semibold text-text">{title}</h2>
          <button
            on:click={close}
            class="p-1 rounded text-text-secondary hover:text-text hover:bg-bg-surface transition-colors"
            aria-label="Close modal"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      {/if}

      <!-- Body -->
      <div class="px-6 py-6 max-h-[480px] overflow-y-auto">
        <slot />
      </div>

      <!-- Footer (optional) -->
      {#if $$slots.footer}
        <div class="flex items-center justify-end gap-3 px-6 h-[60px] border-t border-border-muted">
          <slot name="footer" />
        </div>
      {/if}
    </div>
  </div>
{/if}
