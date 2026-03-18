<svelte:head>
	<title>Dashboard | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import BackupProgressPanel from '$lib/components/shared/BackupProgressPanel.svelte';
	import TrustScoreBadge from '$lib/components/shared/TrustScoreBadge.svelte';
	import CommandStrip from '$lib/components/dashboard/CommandStrip.svelte';
	import StatCard from '$lib/components/ui/StatCard.svelte';
	import { loadDashboard, type DashboardData } from '$lib/api/loaders';
	import { api } from '$lib/api/client';
	import { systemStatus, addToast, backupRunning } from '$lib/stores/app';
	import { subscribe as sseSubscribe } from '$lib/stores/sse';
	import { formatBytes } from '$lib/utils/format';
	import { timeAgo } from '$lib/utils/date';
	import { describeCron } from '$lib/utils/schedule';
	import { onMount, onDestroy } from 'svelte';

	let data: DashboardData | null = null;
	let loading = true;
	let error = '';
	let verifying = false;
	let trustScore: number | null = null;
	let lastVerifiedAt: string | null = null;

	const unsubCompleted = sseSubscribe('backup:completed', () => {
		backupRunning.set(false);
	});
	const unsubFailed = sseSubscribe('backup:failed', () => {
		backupRunning.set(false);
	});
	const unsubCancelled = sseSubscribe('backup:cancelled', () => {
		backupRunning.set(false);
	});
	const unsubVerifyStarted = sseSubscribe('verification:started', () => {
		verifying = true;
	});
	const unsubVerifyCompleted = sseSubscribe('verification:completed', (event) => {
		verifying = false;
		if (event.data.trust_score !== undefined) trustScore = event.data.trust_score as number;
		if (event.data.last_verified_at) lastVerifiedAt = event.data.last_verified_at as string;
	});
	const unsubVerifyFailed = sseSubscribe('verification:failed', () => {
		verifying = false;
		addToast({ type: 'error', message: 'Backup verification failed' });
	});

	onDestroy(() => {
		unsubCompleted();
		unsubFailed();
		unsubCancelled();
		unsubVerifyStarted();
		unsubVerifyCompleted();
		unsubVerifyFailed();
	});

	onMount(() => {
		loadDashboard().then((d) => {
			data = d;
			if (d.status) {
				systemStatus.set(d.status);
				trustScore = d.status.trust_score ?? d.status.verification_status?.trust_score ?? null;
				lastVerifiedAt = d.status.last_verified_at ?? d.status.verification_status?.last_verified_at ?? null;
			}
			loading = false;
		}).catch((e) => {
			console.error('Dashboard load error:', e);
			error = e.message || 'Failed to load dashboard';
			loading = false;
		});
	});

	function getLastBackupTime(status: any): string | null {
		if (!status?.last_backup) return null;
		if (typeof status.last_backup === 'string') return status.last_backup;
		if (status.last_backup?.completed_at) return status.last_backup.completed_at;
		if (status.last_backup?.started_at) return status.last_backup.started_at;
		return null;
	}

	function getLastBackupStatus(status: any): string | null {
		if (status?.last_backup_status) return status.last_backup_status;
		if (status?.last_backup?.status) return status.last_backup.status;
		return null;
	}

	function getContainersDiscovered(status: any): number {
		return status?.containers_discovered ?? status?.databases?.total ?? 0;
	}

	function getDatabasesFound(status: any): number {
		return status?.databases_found ?? status?.databases?.total ?? 0;
	}

	function getStorageUsed(status: any): number {
		return status?.storage_used_bytes ?? status?.storage?.total_bytes ?? 0;
	}

	function getTargetsConfigured(status: any): number {
		return status?.targets_configured ?? status?.targets?.total ?? 0;
	}

	function handleBackup() {
		if (!data?.jobs?.length) {
			addToast({ type: 'warning', message: 'No backup jobs configured' });
			return;
		}
		backupRunning.set(true);
		api.triggerJob(data.jobs[0].id).then((result: any) => {
			addToast({ type: 'success', message: `Backup started (run: ${result.run_id})` });
		}).catch((e: any) => {
			addToast({ type: 'error', message: e.message || 'Failed to trigger backup' });
			backupRunning.set(false);
		});
	}

	function handleVerify() {
		verifying = true;
		api.triggerVerification().then(() => {
			addToast({ type: 'info', message: 'Verification started' });
		}).catch((e: any) => {
			addToast({ type: 'error', message: e.message || 'Failed to start verification' });
			verifying = false;
		});
	}

	$: hostname = data?.status?.hostname ?? '';
	$: resolvedStatus = data?.status?.health ?? 'unknown';
	$: coverageWarnings = data?.status?.coverage?.warnings ?? [];
	$: lastBackupValue = getLastBackupTime(data?.status)
		? timeAgo(getLastBackupTime(data?.status) ?? '')
		: 'Never';
	$: storageValue = formatBytes(getStorageUsed(data?.status));
</script>

<Header title="Dashboard" />

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger/10 border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	{#if data?.error}
		<div class="p-4 bg-danger/10 border border-danger/30 rounded text-danger text-sm">{data.error}</div>
	{/if}

	<!-- Command Strip -->
	{#if !loading}
		<CommandStrip
			{hostname}
			systemStatus={resolvedStatus}
			{coverageWarnings}
			onBackup={handleBackup}
			backupDisabled={$backupRunning}
		/>
	{/if}

	{#if loading}
		<div class="grid grid-cols-1 md:grid-cols-4 gap-4">
			{#each Array(4) as _}
				<div class="bg-bg-surface border border-border rounded-lg p-5 animate-skeleton h-24"></div>
			{/each}
		</div>
	{:else if data}
		<!-- Stat Cards -->
		<div class="grid grid-cols-1 md:grid-cols-4 gap-4">
			<StatCard
				label="System"
				value={data.status?.health ? data.status.health.charAt(0).toUpperCase() + data.status.health.slice(1) : 'Healthy'}
				gradient="from-success to-info"
			>
				<p class="text-xs text-text-secondary mt-1">{data.status?.platform || 'unraid'} &middot; v{data.status?.version || '0.1.0'}</p>
			</StatCard>

			<StatCard
				label="Last Backup"
				value={lastBackupValue}
				gradient="from-primary to-primary-hover"
			>
				{#if getLastBackupStatus(data.status)}
					<p class="text-xs mt-1">
						<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-success/20 text-success">
							{getLastBackupStatus(data.status)}
						</span>
					</p>
				{:else}
					<p class="text-xs text-text-secondary mt-1">Next: {data.status?.next_backup ? timeAgo(data.status.next_backup) : 'N/A'}</p>
				{/if}
			</StatCard>

			<StatCard
				label="Storage Used"
				value={storageValue}
				gradient="from-warning to-warning-hover"
			>
				<p class="text-xs text-text-secondary mt-1">{data.status?.total_snapshots || 0} snapshots across {getTargetsConfigured(data.status)} targets</p>
			</StatCard>

			<div class="card relative overflow-hidden">
				<div class="absolute top-0 inset-x-0 h-0.5 bg-gradient-to-r {trustScore !== null && trustScore >= 80 ? 'from-success to-success' : trustScore !== null && trustScore >= 50 ? 'from-warning to-warning' : trustScore !== null ? 'from-danger to-danger' : 'from-border-muted to-border-muted'}"></div>
				<p class="text-xs uppercase tracking-wider text-text-secondary mb-2">Backup Trust Score</p>
				<div class="flex items-center gap-3">
					<TrustScoreBadge score={trustScore} {lastVerifiedAt} {verifying} />
					<div class="flex flex-col gap-1.5">
						<button
							class="px-3 py-1.5 text-xs font-medium rounded bg-primary/20 text-primary hover:bg-primary/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
							on:click={handleVerify}
							disabled={verifying || $backupRunning}
						>
							{verifying ? 'Verifying...' : 'Verify Now'}
						</button>
					</div>
				</div>
			</div>
		</div>

		{#if data.status?.coverage}
			<div class="rounded-lg border border-border bg-bg-surface p-5">
				<div class="flex items-center justify-between gap-3">
					<div>
						<h3 class="font-semibold text-text">Recovery Coverage</h3>
						<p class="mt-1 text-sm text-text-secondary">
							{data.status.coverage.migration_ready
								? 'This server is covering the essentials for migration and recovery.'
								: 'Arkive is operational, but your current backup scope does not yet cover a full server migration.'}
						</p>
					</div>
					<span class="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium {data.status.coverage.migration_ready ? 'bg-success/20 text-success' : 'bg-warning/20 text-warning'}">
						{data.status.coverage.migration_ready ? 'Migration Ready' : 'Partial Coverage'}
					</span>
				</div>
				<div class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
					<div class="rounded-lg border border-border-muted bg-bg-elevated/50 p-3">
						<div class="text-xs uppercase tracking-wide text-text-muted">Watched Directories</div>
						<div class="mt-1 text-lg font-semibold text-text">{data.status.coverage.watched_directories}</div>
					</div>
					<div class="rounded-lg border border-border-muted bg-bg-elevated/50 p-3">
						<div class="text-xs uppercase tracking-wide text-text-muted">Appdata</div>
						<div class="mt-1 text-lg font-semibold {data.status.coverage.appdata_protected ? 'text-success' : 'text-warning'}">
							{data.status.coverage.appdata_protected ? 'Protected' : 'Missing'}
						</div>
					</div>
					<div class="rounded-lg border border-border-muted bg-bg-elevated/50 p-3">
						<div class="text-xs uppercase tracking-wide text-text-muted">Flash Backup</div>
						<div class="mt-1 text-lg font-semibold {data.status.coverage.flash_protected ? 'text-success' : 'text-warning'}">
							{data.status.coverage.flash_protected ? 'Protected' : 'Missing'}
						</div>
					</div>
				</div>
				{#if data.status.coverage.warnings?.length}
					<div class="mt-4 space-y-2">
						{#each data.status.coverage.warnings as warning}
							<div class="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-text-secondary">
								{warning}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		{/if}

		<!-- Backup Progress -->
		<BackupProgressPanel />

		<!-- Two columns: Jobs + Activity -->
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
			<div class="bg-bg-surface border border-border rounded-lg p-5">
				<div class="flex items-center justify-between mb-4">
					<h3 class="font-semibold text-text">Backup Jobs</h3>
					<a href="/settings/jobs" class="text-xs text-primary hover:text-primary-hover">Manage</a>
				</div>
				{#if data.jobs.length === 0}
					<p class="text-sm text-text-secondary">No backup jobs configured.</p>
				{:else}
					<div class="space-y-3">
						{#each data.jobs as job}
							<div class="flex items-center justify-between py-2 border-b border-border last:border-0">
								<div>
									<p class="text-sm font-medium text-text">{job.name}</p>
									<p class="text-xs text-text-secondary">{describeCron(job.schedule)}</p>
									<p class="text-[11px] text-text-muted font-mono mt-0.5">{job.schedule}</p>
								</div>
								<div class="flex items-center gap-2">
									{#if job.last_run?.status}
										<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium
											{job.last_run.status === 'success' ? 'bg-success/20 text-success' :
											 job.last_run.status === 'failed' ? 'bg-danger/20 text-danger' :
											 job.last_run.status === 'running' ? 'bg-primary/20 text-primary' :
											 'bg-warning/20 text-warning'}">{job.last_run.status}</span>
									{:else if job.last_status}
										<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium
											{job.last_status === 'success' ? 'bg-success/20 text-success' :
											 job.last_status === 'failed' ? 'bg-danger/20 text-danger' :
											 job.last_status === 'running' ? 'bg-primary/20 text-primary' :
											 'bg-warning/20 text-warning'}">{job.last_status}</span>
									{/if}
									<span class="text-xs {job.enabled ? 'text-success' : 'text-text-secondary line-through'}">{job.enabled ? 'Active' : 'Paused'}</span>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<div class="bg-bg-surface border border-border rounded-lg p-5">
				<div class="flex items-center justify-between mb-4">
					<h3 class="font-semibold text-text">Recent Activity</h3>
					<a href="/activity" class="text-xs text-primary hover:text-primary-hover">View All</a>
				</div>
				{#if data.activity.length === 0}
					<div class="rounded-lg border border-dashed border-border-muted bg-bg-elevated/50 p-4">
						<p class="text-sm text-text-secondary">No activity yet.</p>
						<p class="text-xs text-text-muted mt-1">Run your first backup or test a target to populate the activity feed.</p>
					</div>
				{:else}
					<div class="space-y-3">
						{#each data.activity as entry}
							<div class="flex items-start gap-3 py-2 border-b border-border last:border-0">
								<div class="w-1.5 h-1.5 rounded-full mt-2 shrink-0 {
									entry.level === 'error' ? 'bg-danger' :
									entry.level === 'warning' ? 'bg-warning' :
									entry.level === 'success' ? 'bg-success' : 'bg-primary'
								}"></div>
								<div class="min-w-0">
									<p class="text-sm text-text truncate">{entry.message}</p>
									<p class="text-xs text-text-secondary">{timeAgo(entry.timestamp)}</p>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</main>
