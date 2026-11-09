<script lang="ts">
  import ModalShell from './ModalShell.svelte';
  import { createEventDispatcher } from 'svelte';

  export let open: boolean = false;
  export let title: string = 'Confirm Deletion';
  export let message: string = 'This action cannot be undone.';
  export let confirmText: string = 'DELETE';
  export let confirmLabel: string = 'Delete';
  export let loading: boolean = false;
  let className: string = '';
  export { className as class };

  const dispatch = createEventDispatcher();

  let typed = '';

  $: confirmed = typed === confirmText;

  function handleConfirm() {
    if (confirmed && !loading) {
      dispatch('confirm');
    }
  }

  function handleClose() {
    typed = '';
    open = false;
    dispatch('cancel');
  }

  $: if (!open) {
    typed = '';
  }
</script>

<ModalShell bind:open {title} on:close={handleClose} maxWidth="max-w-md" danger={true} class={className}>
  <div class="space-y-4">
    <div class="p-3 rounded-lg bg-danger-bg/50">
      <p class="text-sm text-danger">{message}</p>
    </div>

    <div>
      <label for="confirm-input" class="block text-xs font-semibold uppercase tracking-wide text-text-secondary mb-1.5">
        Type <span class="font-mono text-text">{confirmText}</span> to confirm
      </label>
      <input
        id="confirm-input"
        type="text"
        bind:value={typed}
        on:keydown={(e) => e.key === 'Enter' && handleConfirm()}
        placeholder={confirmText}
        class="w-full h-9 bg-bg-input border border-border rounded-md px-3 text-sm font-mono text-text
               focus:border-danger focus:ring-2 focus:ring-danger/30 outline-hidden transition-colors"
      />
    </div>
  </div>

  <svelte:fragment slot="footer">
    <button on:click={handleClose} class="btn-secondary btn-sm">Cancel</button>
    <button
      on:click={handleConfirm}
      disabled={!confirmed || loading}
      class="btn-danger-solid btn-sm disabled:opacity-50"
    >
      {#if loading}Deleting...{:else}{confirmLabel}{/if}
    </button>
  </svelte:fragment>
</ModalShell>
