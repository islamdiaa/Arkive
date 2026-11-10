<svelte:head>
	<title>Browse Backups | Arkive</title>
</svelte:head>

<script lang="ts">
  import Header from '$lib/components/layout/Header.svelte';
  import EmptyState from '$lib/components/shared/EmptyState.svelte';
  import SnapshotBrowser from '$lib/components/modals/SnapshotBrowser.svelte';
  import RestoreModal from '$lib/components/modals/RestoreModal.svelte';
  import { formatBytes } from '$lib/utils/format';
  import { timeAgo } from '$lib/utils/date';
  import { api } from '$lib/api/client';
  import { onMount } from 'svelte';
  import type { Snapshot, FileNode } from '$lib/types';

  let snapshots: Snapshot[] = [];
  let loading = true;
  let error = '';
  let selectedSnapshot: Snapshot | null = null;
  let showBrowser = false;
  let showRestore = false;
  let browserNodes: FileNode[] = [];

  async function openBrowser(snapshot: Snapshot) {
    selectedSnapshot = snapshot;
    try {
      const data = await api.browseSnapshot(snapshot.id, '/');
      browserNodes = data.entries || data.files || [];
    } catch (e) {
      console.error('Failed to browse snapshot:', e);
      browserNodes = [];
    }
    showBrowser = true;
  }

  onMount(async () => {
    try {
      const result = await api.listSnapshots();
      snapshots = result.items || result.snapshots || [];
    } catch (e: any) {
      error = e.message || 'Failed to load snapshots';
    }
    loading = false;
  });
</script>

<Header title="Restore" />

<main class="p-6 space-y-6">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <h2 class="text-2xl font-semibold text-text">Browse & Restore</h2>
      <p class="text-sm text-text-secondary mt-1">Select a snapshot to browse files and restore</p>
    </div>
    <a href="/restore/database" class="btn-secondary text-sm">Restore a database</a>
  </div>

  {#if error}
    <div class="p-4 bg-danger/10 border border-danger/30 rounded text-danger text-sm">{error}</div>
  {/if}

  {#if loading}
    <div class="space-y-2">
      {#each Array(3) as _}
        <div class="h-20 bg-surface border border-border rounded-lg animate-skeleton"></div>
      {/each}
    </div>
  {:else if snapshots.length === 0}
    <EmptyState icon="camera" title="No snapshots available" description="Run a backup first to create snapshots you can browse and restore from." />
  {:else}
    <div class="grid gap-3">
      {#each snapshots as snap}
        <div class="bg-surface border border-border rounded-lg p-4 flex items-center justify-between hover:border-primary/30 transition-colors">
          <div class="flex items-center gap-4">
            <div class="w-10 h-10 rounded-md bg-primary/10 flex items-center justify-center font-mono text-primary text-sm">{(snap.short_id || snap.id || '----').slice(0, 4)}</div>
            <div>
              <div class="text-sm font-medium text-text">{snap.hostname || 'unknown'}</div>
              <div class="text-xs text-text-secondary">{timeAgo(snap.time)} · {snap.target_id || ''} · {formatBytes(snap.size_bytes || 0)}</div>
            </div>
          </div>
          <div class="flex gap-2">
            <button
              on:click={() => openBrowser(snap)}
              class="text-xs px-3 py-1.5 rounded border border-border text-text-secondary hover:text-primary hover:border-primary transition-colors"
            >Browse Files</button>
            <button
              on:click={() => { selectedSnapshot = snap; showRestore = true; }}
              class="text-xs px-3 py-1.5 rounded bg-primary text-white hover:bg-primary/90 transition-colors"
            >Restore</button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</main>

<SnapshotBrowser bind:open={showBrowser} snapshot={selectedSnapshot} nodes={browserNodes} />
<RestoreModal bind:open={showRestore} snapshot={selectedSnapshot} />
