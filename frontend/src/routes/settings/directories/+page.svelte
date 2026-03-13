<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { onMount } from 'svelte';
	import { FolderOpen, Search, Plus, Check, ShieldCheck, ShieldAlert, Shield } from 'lucide-svelte';
	import FormInput from '$lib/components/ui/FormInput.svelte';
	import StatusBadge from '$lib/components/shared/StatusBadge.svelte';

	let directories: any[] = [];
	let suggestions: any[] = [];
	let loading = true;
	let scanning = false;
	let scanned = false;
	let error = '';
	let newPath = '';
	let newLabel = '';
	let addingPaths = new Set<string>();
	let addingManual = false;
	let removingId = '';

	function formatBytes(bytes: number): string {
		if (!bytes || bytes <= 0) return '0 B';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
		return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
	}

	onMount(async () => {
		try {
			const result = await api.listDirectories();
			directories = result.directories || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load directories';
		}
		loading = false;
	});

	async function scanForSuggestions() {
		if (scanning) return;
		scanning = true;
		try {
			const result = await api.scanDirectories();
			suggestions = (result.suggestions || []).filter((s: any) => !s.already_watched);
			scanned = true;
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Scan failed' });
		}
		scanning = false;
	}

	async function addSuggestion(suggestion: any) {
		if (addingPaths.has(suggestion.path)) return;
		addingPaths.add(suggestion.path);
		addingPaths = addingPaths; // trigger reactivity
		try {
			await api.addDirectory({
				path: suggestion.path,
				label: suggestion.label,
				exclude_patterns: suggestion.recommended_excludes || [],
				enabled: true
			});
			addToast({ type: 'success', message: `Added "${suggestion.label}"` });
			// Remove from suggestions locally instead of re-scanning.
			suggestions = suggestions.filter((s: any) => s.path !== suggestion.path);
			// Refresh the directories list.
			const dirResult = await api.listDirectories();
			directories = dirResult.directories || [];
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Failed to add directory' });
		}
		addingPaths.delete(suggestion.path);
		addingPaths = addingPaths; // trigger reactivity
	}

	async function addDir() {
		if (addingManual) return;
		addingManual = true;
		const addedPath = newPath;
		try {
			await api.addDirectory({
				path: newPath,
				label: newLabel || newPath.split('/').pop() || newPath,
				exclude_patterns: [],
				enabled: true
			});
			addToast({ type: 'success', message: 'Directory added' });
			newPath = '';
			newLabel = '';
			const result = await api.listDirectories();
			directories = result.directories || [];
			// Remove from suggestions if it was there.
			if (scanned) {
				suggestions = suggestions.filter((s: any) => s.path !== addedPath);
			}
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Failed to add directory' });
		}
		addingManual = false;
	}

	async function removeDir(id: string) {
		if (removingId) return;
		removingId = id;
		try {
			await api.deleteDirectory(id);
			directories = directories.filter(d => d.id !== id);
			addToast({ type: 'success', message: 'Directory removed' });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Failed to remove directory' });
		}
		removingId = '';
	}
</script>

<Header title="Watched Directories" />

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm" role="alert">{error}</div>
	{/if}

	<!-- Discover Suggestions -->
	<div class="card">
		<div class="flex items-center justify-between mb-4">
			<div>
				<h3 class="font-semibold text-text">Discover Directories</h3>
				<p class="text-xs text-text-secondary mt-1">Scan your server for directories that should be backed up.</p>
			</div>
			<button on:click={scanForSuggestions} disabled={scanning} class="btn-primary flex items-center gap-2 disabled:opacity-50">
				<Search size={14} />
				{scanning ? 'Scanning...' : 'Scan'}
			</button>
		</div>

		{#if scanning}
			<div class="space-y-3">
				{#each Array(3) as _}
					<div class="rounded-lg border border-border bg-bg-elevated/50 animate-skeleton h-16"></div>
				{/each}
			</div>
		{:else if scanned && suggestions.length === 0}
			<div class="rounded-lg border border-success/30 bg-success/10 p-4 text-center">
				<Check size={24} class="text-success mx-auto mb-2" />
				<p class="text-sm text-text">All discovered directories are already watched.</p>
			</div>
		{:else if suggestions.length > 0}
			<div class="space-y-2">
				{#each suggestions as suggestion}
					<div class="rounded-lg border border-border bg-bg-elevated/50 p-3 flex items-center justify-between gap-3">
						<div class="flex items-center gap-3 min-w-0">
							<div class="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0
								{suggestion.priority === 'critical' ? 'bg-danger/10 border border-danger/30' :
								 suggestion.priority === 'recommended' ? 'bg-primary/10 border border-primary/30' :
								 'bg-bg-surface border border-border'}">
								{#if suggestion.priority === 'critical'}
									<ShieldAlert size={14} class="text-danger" />
								{:else if suggestion.priority === 'recommended'}
									<ShieldCheck size={14} class="text-primary" />
								{:else}
									<Shield size={14} class="text-text-secondary" />
								{/if}
							</div>
							<div class="min-w-0">
								<div class="flex items-center gap-2">
									<p class="text-sm font-medium text-text">{suggestion.label}</p>
									<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide
										{suggestion.priority === 'critical' ? 'bg-danger/20 text-danger' :
										 suggestion.priority === 'recommended' ? 'bg-primary/20 text-primary' :
										 'bg-bg-surface text-text-secondary'}">
										{suggestion.priority}
									</span>
								</div>
								<p class="text-xs text-text-secondary font-mono mt-0.5 truncate">{suggestion.path}</p>
								<p class="text-[11px] text-text-muted mt-0.5">
									{suggestion.reason}
									{#if suggestion.size_bytes > 0}
										&middot; {formatBytes(suggestion.size_bytes)}
									{/if}
									{#if suggestion.file_count > 0}
										&middot; {suggestion.file_count.toLocaleString()}{suggestion.file_count >= 5000 ? '+' : ''} files
									{/if}
								</p>
							</div>
						</div>
						<button
							on:click={() => addSuggestion(suggestion)}
							disabled={addingPaths.has(suggestion.path)}
							class="btn-primary flex items-center gap-1.5 text-xs px-3 py-1.5 flex-shrink-0 disabled:opacity-50"
						>
							<Plus size={12} />
							{addingPaths.has(suggestion.path) ? 'Adding...' : 'Add'}
						</button>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Manual Add -->
	<div class="card">
		<h3 class="font-semibold text-text mb-4">Add Directory Manually</h3>
		<div class="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
			<div class="md:col-span-1">
				<FormInput
					id="new-dir-path"
					label="Path"
					bind:value={newPath}
					placeholder="/mnt/user/scripts"
					mono={true}
				/>
			</div>
			<div class="md:col-span-1">
				<FormInput
					id="new-dir-label"
					label="Label (optional)"
					bind:value={newLabel}
					placeholder="Scripts"
				/>
			</div>
			<div>
				<button on:click={addDir} disabled={!newPath || addingManual} class="btn-primary w-full disabled:opacity-50">
					{addingManual ? 'Adding...' : 'Add'}
				</button>
			</div>
		</div>
	</div>

	<!-- Watched Directories List -->
	{#if loading}
		<div class="space-y-3">
			{#each Array(3) as _}
				<div class="card animate-skeleton h-16"></div>
			{/each}
		</div>
	{:else if directories.length === 0}
		<div class="card text-center py-12">
			<FolderOpen size={48} class="text-text-secondary mx-auto mb-4" />
			<p class="text-sm text-text-secondary">No directories configured. Use Scan above to discover directories to protect.</p>
		</div>
	{:else}
		<div>
			<h3 class="font-semibold text-text mb-3">Watched Directories ({directories.length})</h3>
			<div class="space-y-3">
				{#each directories as dir}
					<div class="card flex items-center justify-between">
						<div class="flex items-center gap-3 min-w-0">
							<div class="w-8 h-8 rounded-lg bg-bg-surface border border-border flex items-center justify-center flex-shrink-0">
								<FolderOpen size={14} class="text-text-secondary" />
							</div>
							<div class="min-w-0">
								<p class="text-sm font-medium text-text">{dir.label || dir.path}</p>
								<p class="text-xs text-text-secondary font-mono mt-0.5 truncate">{dir.path}</p>
							</div>
						</div>
						<div class="flex items-center gap-3">
							<StatusBadge status={dir.enabled ? 'success' : 'warning'} size="sm" />
							<button
								on:click={() => removeDir(dir.id)}
								disabled={removingId === dir.id}
								class="text-text-secondary hover:text-danger disabled:opacity-50"
								aria-label="Remove directory"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
							</button>
						</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</main>
