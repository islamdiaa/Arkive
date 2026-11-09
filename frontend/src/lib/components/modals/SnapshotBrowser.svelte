<script lang="ts">
  import type { FileNode, Snapshot } from '$lib/types';
  import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogDescription,
    DialogFooter,
  } from '$lib/components/ui/dialog';

  export let open: boolean = false;
  export let snapshot: Snapshot | null = null;
  export let nodes: FileNode[] = [];
  export let loading: boolean = false;

  let selectedPaths: string[] = [];

  function togglePath(path: string) {
    if (selectedPaths.includes(path)) selectedPaths = selectedPaths.filter(p => p !== path);
    else selectedPaths = [...selectedPaths, path];
  }

  function cancel() { open = false; }
</script>

<Dialog bind:open>
  <DialogContent class="max-w-2xl bg-surface border-border max-h-[85vh] flex flex-col">
    <DialogHeader>
      <DialogTitle class="text-text">Browse Snapshot</DialogTitle>
      {#if snapshot}
        <DialogDescription class="font-mono text-text-secondary">
          {snapshot.short_id || snapshot.id?.slice(0, 8)} -- {snapshot.time}
        </DialogDescription>
      {/if}
    </DialogHeader>

    <div class="flex-1 overflow-y-auto border border-border rounded-md bg-page p-2 min-h-[300px]">
      {#if loading}
        <div class="flex items-center justify-center py-8 text-text-secondary">Loading...</div>
      {:else if nodes.length === 0}
        <div class="flex items-center justify-center py-8 text-text-secondary">No files found</div>
      {:else}
        {#each nodes as node}
          <!-- A label row: one tab stop (the checkbox), Space toggles it, and
               the whole row is the click target. The checkbox used to sit
               inside a role="button" row and swallowed its own clicks, so
               clicking the box itself never changed the selection. -->
          <label class="flex items-center gap-2 px-2 py-1.5 hover:bg-surface rounded text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={selectedPaths.includes(node.path || node.name)}
              on:change={() => togglePath(node.path || node.name)}
              class="h-4 w-4 rounded"
            />
            <span class="text-text-secondary" aria-hidden="true">{node.type === 'dir' ? '/' : ''}</span>
            <span class="text-text">{node.name}{#if node.type === 'dir'}<span class="sr-only"> (folder)</span>{/if}</span>
          </label>
        {/each}
      {/if}
    </div>

    <DialogFooter class="justify-between items-center">
      <span class="text-xs text-text-secondary">{selectedPaths.length} selected</span>
      <button on:click={cancel} class="px-4 py-2 text-sm rounded-md border border-border text-text-secondary hover:text-text transition-colors">Close</button>
    </DialogFooter>
  </DialogContent>
</Dialog>
