<svelte:head>
	<title>Snapshot Details | Arkive</title>
</svelte:head>

<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import Header from '$lib/components/layout/Header.svelte';
  import RestoreModal from '$lib/components/modals/RestoreModal.svelte';
  import { api } from '$lib/api/client';
  import { formatBytes } from '$lib/utils/format';
  import { timeAgo } from '$lib/utils/date';
  import type { Snapshot } from '$lib/types';

  export let data: { id?: string } = {};

  let snapshotId: string = '';
  let snapshot: Snapshot | null = null;
  let loading = true;
  let error = '';
  let browseEntries: Array<{ name: string; type: string; size?: number }> = [];
  let browsePath = '/';
  let browseLoading = false;
  let showRestore = false;

  $: snapshotId = $page.params.id || data.id || '';

  onMount(async () => {
    if (!snapshotId) {
      error = 'No snapshot ID provided.';
      loading = false;
      return;
    }
    await loadSnapshot();
  });

  async function loadSnapshot() {
    loading = true;
    error = '';
    try {
      // Fetch snapshot by ID directly
      snapshot = await api.get<Snapshot>(`/snapshots/${encodeURIComponent(snapshotId)}`);
      if (!snapshot) {
        error = `Snapshot "${snapshotId}" not found. It may have been pruned.`;
      } else {
        await browsePath_load('/');
      }
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : 'Failed to load snapshot.';
    } finally {
      loading = false;
    }
  }

  async function browsePath_load(path: string) {
    if (!snapshot) return;
    browseLoading = true;
    browseEntries = [];
    try {
      const result = await api.get<{ path: string; entries: typeof browseEntries }>(
        `/snapshots/${encodeURIComponent(snapshot.id)}/browse?path=${encodeURIComponent(path)}&target_id=${encodeURIComponent(snapshot.target_id || '')}`
      );
      browseEntries = result.entries || [];
      browsePath = path;
    } catch {
      browseEntries = [];
    } finally {
      browseLoading = false;
    }
  }

  async function navigateTo(entry: { name: string; type: string }) {
    if (entry.type !== 'dir' && entry.type !== 'directory') return;
    const newPath = browsePath === '/' ? `/${entry.name}` : `${browsePath}/${entry.name}`;
    await browsePath_load(newPath);
  }

  async function navigateUp() {
    if (browsePath === '/') return;
    const parts = browsePath.split('/').filter(Boolean);
    parts.pop();
    const parent = parts.length === 0 ? '/' : '/' + parts.join('/');
    await browsePath_load(parent);
  }

  function formatEntryType(type: string): string {
    return (type === 'dir' || type === 'directory') ? 'Directory' : 'File';
  }

  function isDir(type: string): boolean {
    return type === 'dir' || type === 'directory';
  }
</script>

<Header title="Snapshot Detail" />

<main class="p-6 space-y-6">

  <!-- Breadcrumb -->
  <nav class="flex items-center gap-2 text-sm text-text-secondary">
    <a href="/snapshots" class="hover:text-primary transition-colors">Snapshots</a>
    <span>/</span>
    <span class="text-text font-mono">{snapshotId}</span>
  </nav>

  {#if loading}
    <!-- Skeleton loader -->
    <div class="space-y-4">
      <div class="h-32 bg-surface border border-border rounded-lg animate-skeleton"></div>
      <div class="h-48 bg-surface border border-border rounded-lg animate-skeleton"></div>
    </div>

  {:else if error}
    <!-- Error state -->
    <div class="bg-surface border border-danger/40 rounded-lg p-6 text-center">
      <div class="text-danger text-lg font-semibold mb-2">Snapshot Not Found</div>
      <p class="text-text-secondary text-sm">{error}</p>
      <a
        href="/snapshots"
        class="inline-block mt-4 px-4 py-2 rounded bg-primary/10 text-primary text-sm hover:bg-primary/20 transition-colors"
      >
        Back to Snapshots
      </a>
    </div>

  {:else if snapshot}
    <!-- Snapshot metadata card -->
    <div class="bg-surface border border-border rounded-lg p-6 space-y-4">
      <div class="flex items-start justify-between">
        <div>
          <div class="flex items-center gap-3">
            <span class="font-mono text-lg text-primary font-semibold">
              {snapshot.short_id || snapshot.id?.slice(0, 8)}
            </span>
            {#if snapshot.tags?.length}
              <div class="flex gap-1">
                {#each snapshot.tags as tag}
                  <span class="text-xs px-1.5 py-0.5 rounded bg-primary/10 text-primary">{tag}</span>
                {/each}
              </div>
            {/if}
          </div>
          <p class="text-xs text-text-secondary mt-1 font-mono">{snapshot.id}</p>
        </div>

        <!-- Restore button -->
        <button
          on:click={() => { showRestore = true; }}
          class="px-4 py-2 rounded bg-primary text-white text-sm font-medium hover:bg-primary/80 transition-colors"
        >
          Restore Snapshot
        </button>
      </div>

      <!-- Metadata grid -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 pt-2 border-t border-border">
        <div>
          <div class="text-xs text-text-secondary uppercase tracking-wide mb-1">Date</div>
          <div class="text-sm text-text">{timeAgo(snapshot.time)}</div>
          <div class="text-xs text-text-secondary font-mono">{new Date(snapshot.time).toLocaleString()}</div>
        </div>
        <div>
          <div class="text-xs text-text-secondary uppercase tracking-wide mb-1">Size</div>
          <div class="text-sm text-text">{formatBytes(snapshot.size_bytes || 0)}</div>
        </div>
        <div>
          <div class="text-xs text-text-secondary uppercase tracking-wide mb-1">Hostname</div>
          <div class="text-sm text-text font-mono">{snapshot.hostname || 'unknown'}</div>
        </div>
        <div>
          <div class="text-xs text-text-secondary uppercase tracking-wide mb-1">Target</div>
          <div class="text-sm text-text">{snapshot.target_id || '—'}</div>
        </div>
      </div>

      <!-- Paths -->
      {#if snapshot.paths?.length}
        <div class="pt-2 border-t border-border">
          <div class="text-xs text-text-secondary uppercase tracking-wide mb-2">Backed-up Paths</div>
          <div class="flex flex-wrap gap-2">
            {#each snapshot.paths as p}
              <span class="text-xs font-mono px-2 py-1 rounded bg-page border border-border text-text-secondary">{p}</span>
            {/each}
          </div>
        </div>
      {/if}
    </div>

    <!-- File browser -->
    <div class="bg-surface border border-border rounded-lg overflow-hidden">
      <div class="flex items-center justify-between px-4 py-3 border-b border-border">
        <div class="flex items-center gap-2">
          <span class="text-sm font-medium text-text">Browse Files</span>
          <span class="text-xs font-mono text-text-secondary bg-page px-2 py-0.5 rounded border border-border">
            {browsePath}
          </span>
        </div>
        {#if browsePath !== '/'}
          <button
            on:click={navigateUp}
            class="text-xs px-2 py-1 rounded border border-border text-text-secondary hover:text-primary hover:border-primary transition-colors"
          >
            Up
          </button>
        {/if}
      </div>

      {#if browseLoading}
        <div class="p-6 space-y-2">
          {#each Array(4) as _}
            <div class="h-8 bg-page rounded animate-skeleton"></div>
          {/each}
        </div>

      {:else if browseEntries.length === 0}
        <div class="p-6 text-center text-text-secondary text-sm">
          No files found at this path.
        </div>

      {:else}
        <div class="divide-y divide-border">
          {#each browseEntries as entry}
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <div
              class="flex items-center justify-between px-4 py-2.5 hover:bg-page/50 transition-colors
                     {isDir(entry.type) ? 'cursor-pointer' : ''}"
              on:click={() => navigateTo(entry)}
              on:keydown={(e) => e.key === 'Enter' && navigateTo(entry)}
              role={isDir(entry.type) ? 'button' : 'row'}
              tabindex={isDir(entry.type) ? 0 : -1}
            >
              <div class="flex items-center gap-3">
                <!-- Icon -->
                {#if isDir(entry.type)}
                  <svg class="w-4 h-4 text-folder flex-shrink-0" fill="currentColor" viewBox="0 0 16 16">
                    <path d="M.5 3l.04.87a1.99 1.99 0 00-.342 1.311l.637 7A2 2 0 002.826 14H13.174a2 2 0 001.991-1.819l.637-7A2 2 0 0013.84 3H9.828a2 2 0 01-1.414-.586l-.828-.828A2 2 0 006.172 1H2.5a2 2 0 00-2 2z"/>
                  </svg>
                {:else}
                  <svg class="w-4 h-4 text-text-secondary flex-shrink-0" fill="currentColor" viewBox="0 0 16 16">
                    <path d="M4 0a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V4.707A1 1 0 0013.707 4L10 .293A1 1 0 009.293 0H4zm5.5 1.5v2a1 1 0 001 1h2l-3-3z"/>
                  </svg>
                {/if}
                <span class="text-sm font-mono text-text">{entry.name}</span>
              </div>
              <div class="flex items-center gap-4">
                {#if entry.size !== undefined && !isDir(entry.type)}
                  <span class="text-xs text-text-secondary">{formatBytes(entry.size)}</span>
                {/if}
                <span class="text-xs text-text-secondary">{formatEntryType(entry.type)}</span>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Restore dialog: restores into a safe restore root, never over the originals -->
  <RestoreModal bind:open={showRestore} {snapshot} />

</main>
