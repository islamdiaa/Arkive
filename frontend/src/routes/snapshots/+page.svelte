<svelte:head>
	<title>Snapshots | Arkive</title>
</svelte:head>

<script lang="ts">
  import Header from '$lib/components/layout/Header.svelte';
  import EmptyState from '$lib/components/shared/EmptyState.svelte';
  import FilterBar from '$lib/components/ui/FilterBar.svelte';
  import StatusBadge from '$lib/components/shared/StatusBadge.svelte';
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
  let filterTarget = '';
  let filterSearch = '';
  let filterValues: Record<string, string> = {};

  $: filtered = snapshots.filter(s => {
    const targetMatch = !filterTarget && !filterValues.target
      ? true
      : (filterValues.target || filterTarget)
        ? s.target_id === (filterValues.target || filterTarget)
        : true;
    const searchMatch = !filterSearch
      ? true
      : (s.short_id || s.id || '').toLowerCase().includes(filterSearch.toLowerCase()) ||
        (s.hostname || '').toLowerCase().includes(filterSearch.toLowerCase()) ||
        (s.target_id || '').toLowerCase().includes(filterSearch.toLowerCase());
    return targetMatch && searchMatch;
  });

  $: targets = [...new Set(snapshots.map(s => s.target_id))];

  $: targetFilterOptions = targets.map(t => ({ value: t, label: t }));

  function handleFilterChange(e: CustomEvent) {
    filterSearch = e.detail.searchValue;
    filterValues = e.detail.filterValues;
    filterTarget = filterValues.target || '';
  }

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
      snapshots = result.snapshots || result.items || [];
    } catch (e: any) {
      error = e.message || 'Failed to load snapshots';
    }
    loading = false;
  });
</script>

<Header title="Snapshots" />

<main class="p-6 space-y-6">
  {#if error}
    <div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
  {/if}

  <div class="flex items-center justify-between">
    <p class="text-sm text-text-secondary">Browse and restore from backup snapshots</p>
    <span class="text-xs text-text-tertiary">{filtered.length} of {snapshots.length} snapshots</span>
  </div>

  <FilterBar
    class="mb-6"
    searchPlaceholder="Search by ID, host, or target…"
    bind:searchValue={filterSearch}
    bind:filterValues
    filters={targets.length > 0 ? [
      { key: 'target', label: 'Target', options: targetFilterOptions }
    ] : []}
    on:change={handleFilterChange}
  />

  {#if loading}
    <div class="space-y-2">
      {#each Array(5) as _}
        <div class="h-16 bg-bg-surface border border-border rounded-lg animate-skeleton"></div>
      {/each}
    </div>
  {:else if filtered.length === 0}
    <EmptyState icon="camera" title="No snapshots yet" description="Snapshots will appear here after your first backup completes." />
  {:else}
    <div class="card overflow-hidden p-0">
      {#each filtered as snap}
        <div class="flex items-center justify-between px-4 py-3 border-b border-border last:border-0 hover:bg-bg-surface-hover/50 transition-colors">
          <div class="flex items-center gap-4 min-w-0">
            <span class="font-mono text-sm text-primary shrink-0">{snap.short_id || (snap.id?.slice(0, 8) ?? '')}</span>
            <div class="min-w-0">
              <div class="text-sm text-text">{snap.hostname || 'unknown'}</div>
              <div class="text-xs text-text-secondary font-mono">
                {timeAgo(snap.time)}
                {#if snap.target_id}
                  · <span class="text-text-tertiary">{snap.target_id}</span>
                {/if}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-3 shrink-0">
            <span class="font-mono text-xs text-text-secondary">{formatBytes(snap.size_bytes || 0)}</span>
            {#if snap.tags?.length}
              <div class="flex gap-1">
                {#each snap.tags as tag}
                  <span class="text-xs px-1.5 py-0.5 rounded bg-primary-bg text-primary">{tag}</span>
                {/each}
              </div>
            {/if}
            <button
              on:click={() => openBrowser(snap)}
              class="text-xs px-2 py-1 rounded border border-border text-text-secondary hover:text-primary hover:border-primary transition-colors"
            >Browse</button>
            <button
              on:click={() => { selectedSnapshot = snap; showRestore = true; }}
              class="text-xs px-2 py-1 rounded bg-primary-bg text-primary hover:bg-primary/20 transition-colors"
            >Restore</button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</main>

<SnapshotBrowser bind:open={showBrowser} snapshot={selectedSnapshot} nodes={browserNodes} />
<RestoreModal bind:open={showRestore} snapshot={selectedSnapshot} />
