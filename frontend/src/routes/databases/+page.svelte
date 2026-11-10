<svelte:head>
	<title>Databases | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import DumpSizeTrend from '$lib/components/shared/DumpSizeTrend.svelte';
	import { Database } from 'lucide-svelte';
	// Through `api` (not $lib/api/databases): e2e route globs for
	// "**/api/databases*" also match that module's dev-server URL.
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { formatBytes } from '$lib/utils/format';
	import { formatDate, timeAgo } from '$lib/utils/date';
	import {
		drillSummary,
		formatSizeChange,
		healthStatusStyle,
		indexHealth,
		needsAttention,
	} from '$lib/utils/database-health';
	import type { DatabaseHealth, DatabaseHealthPolicy } from '$lib/types';
	import { onMount } from 'svelte';

	let databases: any[] = [];
	let loading = true;
	let error = '';
	let dumpingDb = '';
	let togglingDb = '';
	let acceptingKey = '';
	let healthItems: DatabaseHealth[] = [];
	let healthPolicy: DatabaseHealthPolicy | null = null;
	let healthError = '';

	const keyOf = (db: any) => `${db.container_name}:${db.db_name}:${db.host_path ?? ''}`;
	$: excludedCount = databases.filter((db) => db.excluded).length;
	$: healthByKey = indexHealth(healthItems);
	$: attentionCount = healthItems.filter(needsAttention).length;
	$: healthyCount = healthItems.filter((h) => h.status === 'ok').length;

	const drillTone: Record<string, string> = {
		success: 'text-success',
		danger: 'text-danger',
		muted: 'text-text-secondary',
	};

	async function loadHealth() {
		try {
			const result: any = await api.getDatabaseHealth();
			healthItems = Array.isArray(result?.items) ? result.items : [];
			healthPolicy = result?.policy ?? null;
			healthError = '';
		} catch (e: any) {
			healthError = e?.message || 'Could not load database health';
		}
	}

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
			await loadHealth();
		} catch (e: any) {
			addToast({ type: 'error', message: e.message || 'Could not update the database' });
		}
		togglingDb = '';
	}

	async function acceptSize(health: DatabaseHealth) {
		acceptingKey = health.key;
		try {
			await api.acceptDatabaseSize(health.key);
			addToast({
				type: 'success',
				message: `${health.db_name}: ${formatBytes(health.size_bytes ?? 0)} is now its normal size`,
			});
			await loadHealth();
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Could not accept the new size' });
		}
		acceptingKey = '';
	}

	onMount(async () => {
		try {
			const [result] = await Promise.all([api.listDatabases(), loadHealth()]);
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
			const [result] = await Promise.all([api.listDatabases(), loadHealth()]);
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

	<div class="flex items-center justify-between gap-4">
		<div class="space-y-1">
			<p class="text-sm text-text-secondary">
				{databases.length} databases discovered across containers{#if excludedCount}, {excludedCount} excluded from backups{/if}
			</p>
			{#if healthItems.length}
				<p class="text-xs text-text-secondary" data-testid="db-health-summary">
					{healthyCount} healthy{#if attentionCount}{' · '}<span class="text-warning">{attentionCount} need{attentionCount === 1 ? 's' : ''} attention</span>{/if}
					{#if healthPolicy && healthPolicy.shrink_alert_percent > 0}
						{' · '}<span class="text-text-muted">a run is flagged when a dump is {healthPolicy.shrink_alert_percent}% or more smaller than its recent median
							(<a href="/settings/monitoring" class="underline hover:text-text">change</a>)</span>
					{/if}
				</p>
			{/if}
			{#if healthError}
				<p class="text-xs text-danger">{healthError}</p>
			{/if}
		</div>
		<button on:click={rescan} class="btn-secondary text-sm shrink-0">Re-scan</button>
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
				{@const health = db.key ? healthByKey.get(db.key) : undefined}
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
									class="py-1 text-xs text-primary hover:text-primary-hover disabled:opacity-50 transition-colors"
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
								class="py-1 text-xs text-text-secondary hover:text-text disabled:opacity-50 transition-colors"
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

					{#if health && !db.excluded}
						{@const style = healthStatusStyle(health.status)}
						{@const drill = drillSummary(health.last_drill)}
						<div class="mt-3 pt-3 border-t border-border-muted space-y-2 text-xs" data-testid="db-health">
							<div class="flex items-center justify-between gap-3">
								<span class="badge {style.badge}" data-testid="db-health-status">{style.label}</span>
								{#if health.trend.length}
									<DumpSizeTrend points={health.trend} />
								{/if}
							</div>
							<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
								<dt class="text-text-secondary">Last good dump</dt>
								<dd class="text-text text-right" title={health.last_success_at ? formatDate(health.last_success_at) : ''}>
									{health.last_success_at ? timeAgo(health.last_success_at) : 'Never'}
								</dd>
								<dt class="text-text-secondary">Dump size</dt>
								<dd class="text-text text-right" data-testid="db-health-size">
									{#if health.size_bytes != null}
										{formatBytes(health.size_bytes)}
										{#if health.shrunk}
											<span class="text-warning">({formatSizeChange(-(health.shrink_percent ?? 0))} vs usual)</span>
										{:else if formatSizeChange(health.size_change_percent)}
											<span class="text-text-muted">({formatSizeChange(health.size_change_percent)})</span>
										{/if}
									{:else}
										—
									{/if}
								</dd>
								<dt class="text-text-secondary">Restorable as of</dt>
								<dd
									class="text-right {health.restorable_as_of ? 'text-text' : 'text-danger'}"
									title={health.restorable_as_of ? formatDate(health.restorable_as_of) : 'No snapshot holds a dump of this database'}
									data-testid="db-health-restorable"
								>
									{health.restorable_as_of ? formatDate(health.restorable_as_of) : 'Not restorable yet'}
								</dd>
								<dt class="text-text-secondary">Last restore drill</dt>
								<dd class="text-right {drillTone[drill.tone]}" title={drill.title} data-testid="db-health-drill">
									{drill.label}{#if health.last_drill?.at}{' · '}<span class="text-text-muted">{timeAgo(health.last_drill.at)}</span>{/if}
								</dd>
							</dl>
							{#each health.issues as issue}
								<p class="{health.status === 'warning' ? 'text-warning' : 'text-danger'} break-words">{issue}</p>
							{/each}
							{#each health.notes as note}
								<p class="text-text-muted break-words">{note}</p>
							{/each}
							{#if health.shrunk}
								<button
									on:click={() => acceptSize(health)}
									disabled={acceptingKey === health.key}
									class="text-xs text-primary hover:text-primary-hover disabled:opacity-50 transition-colors"
									title="The data was removed on purpose: stop flagging runs for this smaller size"
								>
									{acceptingKey === health.key ? 'Saving…' : 'Accept new size'}
								</button>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</main>
