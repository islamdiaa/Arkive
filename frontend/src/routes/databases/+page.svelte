<svelte:head>
	<title>Databases | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { Database } from 'lucide-svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { formatBytes } from '$lib/utils/format';
	import { onMount } from 'svelte';

	let databases: any[] = [];
	let loading = true;
	let error = '';
	let dumpingDb = '';
	let togglingDb = '';

	const keyOf = (db: any) => `${db.container_name}:${db.db_name}:${db.host_path ?? ''}`;
	$: excludedCount = databases.filter((db) => db.excluded).length;

	async function toggleExcluded(db: any) {
		const exclude = !db.excluded;
		togglingDb = keyOf(db);
		try {
			await api.setDatabaseExcluded(db, exclude);
			databases = databases.map((d) => (keyOf(d) === keyOf(db) ? { ...d, excluded: exclude } : d));
			addToast({
				type: 'success',
				message: exclude
					? `${db.db_name} is excluded from backups; its old dumps age out after 7 days`
					: `${db.db_name} will be backed up again from the next run`,
			});
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Could not update the database' });
		}
		togglingDb = '';
	}

	onMount(async () => {
		try {
			const result = await api.listDatabases();
			databases = result.items || result.databases || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load databases';
		}
		loading = false;
	});

	async function dumpDb(db: any) {
		dumpingDb = `${db.container_name}:${db.db_name}`;
		try {
			// Backend returns HTTP 200 with status success|failed|skipped and an error.
			const result = await api.dumpDatabase(db.container_name, db.db_name);
			if (result?.status === 'success') {
				const integrity = result.integrity_check && result.integrity_check !== 'ok' && result.integrity_check !== 'passed'
					? `, integrity: ${result.integrity_check}`
					: '';
				addToast({ type: 'success', message: `Dumped ${db.db_name} (${formatBytes(result.dump_size_bytes || 0)}${integrity})` });
			} else {
				addToast({
					type: result?.status === 'skipped' ? 'warning' : 'error',
					message: `Dump ${result?.status || 'failed'} for ${db.db_name}${result?.error ? `: ${result.error}` : ''}`,
				});
			}
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Dump failed' });
		}
		dumpingDb = '';
	}

	async function rescan() {
		loading = true;
		try {
			await api.runScan();
			const result = await api.listDatabases();
			databases = result.items || result.databases || [];
			addToast({ type: 'success', message: `Found ${databases.length} databases` });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		loading = false;
	}

	const dbTypeColors: Record<string, string> = {
		postgres: 'bg-primary-bg text-primary',
		mariadb: 'bg-warning-bg text-warning',
		mysql: 'bg-warning-bg text-warning',
		sqlite: 'bg-info-bg text-info',
		mongodb: 'bg-success-bg text-success',
		redis: 'bg-danger-bg text-danger',
		influxdb: 'bg-primary-bg text-primary-strong',
	};
</script>

<Header title="Databases" />

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	<div class="flex items-center justify-between">
		<p class="text-sm text-text-secondary">
			{databases.length} databases discovered across containers{#if excludedCount}, {excludedCount} excluded from backups{/if}
		</p>
		<button on:click={rescan} class="btn-secondary text-sm">Re-scan</button>
	</div>

	{#if loading}
		<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
			{#each Array(6) as _}
				<div class="card animate-skeleton h-32"></div>
			{/each}
		</div>
	{:else if databases.length === 0}
		<div class="card text-center py-12">
			<Database class="w-12 h-12 text-text-secondary mx-auto mb-4" strokeWidth={1.5} />
			<p class="text-text-secondary">No databases discovered yet.</p>
			<button on:click={rescan} class="btn-primary mt-4">Run Discovery Scan</button>
		</div>
	{:else}
		<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
			{#each databases as db}
				<div class="card hover:border-border-strong transition-colors cursor-default" class:opacity-60={db.excluded}>
					<div class="flex items-center justify-between mb-3">
						<div class="flex items-center gap-2">
							<Database class="w-4 h-4 text-text-secondary" strokeWidth={1.5} />
							<span class="badge {dbTypeColors[db.db_type] || 'bg-neutral-bg text-neutral'}">{db.db_type}</span>
							{#if db.excluded}
								<span class="badge bg-neutral-bg text-neutral" data-testid="excluded-badge">Excluded</span>
							{/if}
						</div>
						<div class="flex items-center gap-3">
							{#if !db.excluded}
								<button
									on:click={() => dumpDb(db)}
									disabled={dumpingDb === `${db.container_name}:${db.db_name}`}
									class="text-xs text-primary hover:text-primary-hover disabled:opacity-50 transition-colors"
								>
									{dumpingDb === `${db.container_name}:${db.db_name}` ? 'Dumping…' : 'Dump Now'}
								</button>
							{/if}
							<button
								on:click={() => toggleExcluded(db)}
								disabled={togglingDb === keyOf(db)}
								title={db.excluded
									? 'Back up this database again'
									: 'Stop backing up this database (e.g. a cache the app rebuilds)'}
								class="text-xs text-text-secondary hover:text-text disabled:opacity-50 transition-colors"
							>
								{db.excluded ? 'Include' : 'Exclude'}
							</button>
						</div>
					</div>
					<p class="text-sm font-medium text-text">{db.db_name}</p>
					<p class="text-xs text-text-secondary mt-1">{db.container_name}</p>
					{#if db.host_path}
						<p class="text-xs font-mono text-text-secondary mt-2 truncate">{db.host_path}</p>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</main>
