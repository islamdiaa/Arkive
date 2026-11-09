<svelte:head>
	<title>Monitoring | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import SectionCard from '$lib/components/ui/SectionCard.svelte';
	import FormInput from '$lib/components/ui/FormInput.svelte';
	import { api } from '$lib/api/client';
	import {
		describeHeartbeatResult,
		isIntInRange,
		isValidHeartbeatUrl,
		saveSettings,
		testHeartbeat,
	} from '$lib/api/settings';
	import { addToast } from '$lib/stores/app';
	import type { HeartbeatTestResult } from '$lib/types';
	import { onMount } from 'svelte';

	let heartbeatUrl = '';
	let staleHours: number | string = 0;
	let shrinkPercent: number | string = 50;
	let shrinkMinMb: number | string = 1;
	let loading = true;
	let error = '';
	let saving = false;
	let saved = false;
	let testing = false;
	let testResult: HeartbeatTestResult | null = null;
	let testError = '';
	let original = '';

	function snapshotOf(url: string, hours: unknown, percent: unknown, minMb: unknown): string {
		return JSON.stringify({
			heartbeatUrl: url.trim(),
			staleHours: String(hours ?? ''),
			shrinkPercent: String(percent ?? ''),
			shrinkMinMb: String(minMb ?? ''),
		});
	}

	function snapshot(): string {
		return snapshotOf(heartbeatUrl, staleHours, shrinkPercent, shrinkMinMb);
	}

	function numberSetting(value: unknown, fallback: number): number {
		const n = typeof value === 'number' ? value : Number(value);
		return Number.isFinite(n) ? n : fallback;
	}

	$: currentSnapshot = snapshotOf(heartbeatUrl, staleHours, shrinkPercent, shrinkMinMb);
	$: isDirty = !loading && original !== currentSnapshot;
	$: urlError = isValidHeartbeatUrl(heartbeatUrl) ? '' : 'Enter an http:// or https:// URL, or leave empty to disable';
	$: hoursError = isIntInRange(staleHours, 0, 8760) ? '' : 'Enter a whole number of hours from 0 to 8760';
	$: shrinkPercentError = isIntInRange(shrinkPercent, 0, 95) ? '' : 'Enter a whole number from 0 to 95';
	$: shrinkMinError = isIntInRange(shrinkMinMb, 0, 1048576) ? '' : 'Enter a whole number of MB (0 or more)';
	$: formError = urlError || hoursError || shrinkPercentError || shrinkMinError;
	$: canTest = !!heartbeatUrl.trim() && !urlError && !testing;

	onMount(async () => {
		try {
			const data = await api.getSettings();
			heartbeatUrl = data.heartbeat_url ?? '';
			staleHours = typeof data.stale_backup_hours === 'number' ? data.stale_backup_hours : Number(data.stale_backup_hours ?? 0) || 0;
			shrinkPercent = numberSetting(data.dump_shrink_alert_percent, 50);
			shrinkMinMb = numberSetting(data.dump_shrink_min_mb, 1);
			original = snapshot();
		} catch (e: any) {
			error = e?.message || 'Failed to load monitoring settings';
		}
		loading = false;
	});

	async function save() {
		if (formError) {
			addToast({ type: 'error', message: formError });
			return;
		}
		saving = true;
		try {
			await saveSettings({
				heartbeat_url: heartbeatUrl.trim(),
				stale_backup_hours: Number(staleHours),
				dump_shrink_alert_percent: Number(shrinkPercent),
				dump_shrink_min_mb: Number(shrinkMinMb),
			});
			heartbeatUrl = heartbeatUrl.trim();
			original = snapshot();
			saved = true;
			addToast({ type: 'success', message: 'Monitoring settings saved' });
			setTimeout(() => (saved = false), 3000);
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Failed to save monitoring settings' });
		}
		saving = false;
	}

	async function sendTestPing() {
		if (!canTest) return;
		testing = true;
		testResult = null;
		testError = '';
		try {
			testResult = await testHeartbeat(heartbeatUrl);
		} catch (e: any) {
			testError = e?.message || 'Test ping failed';
		}
		testing = false;
	}
</script>

<Header title="Monitoring" />

<div class="p-6 space-y-6">
	<div>
		<h1 class="text-2xl font-semibold text-text">Monitoring</h1>
		<p class="text-sm text-text-secondary mt-1">Get alerted when backups stop happening — even if this server can't tell you itself</p>
	</div>

	{#if error}
		<div class="p-4 bg-danger-bg border border-danger-muted rounded text-danger text-sm">{error}</div>
	{/if}

	{#if loading}
		<div class="card animate-skeleton h-64"></div>
	{:else}
		<SectionCard
			title="Heartbeat (dead man's switch)"
			description="Arkive pings this URL every 15 minutes while all backups are healthy. When Arkive dies, or a backup fails or goes overdue, the pings stop and your monitor alerts you."
		>
			<FormInput
				id="heartbeat-url"
				label="Heartbeat URL"
				bind:value={heartbeatUrl}
				placeholder="https://uptime.example.com/api/push/abc123"
				error={urlError}
				mono={true}
				hint="Leave empty to disable the heartbeat."
			/>

			<div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs text-text-secondary">
				<div class="rounded-md border border-border-muted bg-bg-elevated p-3">
					<p class="font-medium text-text mb-1">Uptime Kuma</p>
					<p>Create a <span class="font-medium text-text">Push</span> monitor with a heartbeat interval of 30–60 minutes and paste its push URL here.</p>
				</div>
				<div class="rounded-md border border-border-muted bg-bg-elevated p-3">
					<p class="font-medium text-text mb-1">healthchecks.io</p>
					<p>Also works: create a check with a period of 30–60 minutes and paste its ping URL here.</p>
				</div>
			</div>

			<p class="text-xs text-text-muted">
				Use an interval longer than 15 minutes so one slow check doesn't page you. The monitor must run somewhere other than this server, or it dies with it.
			</p>

			<div class="flex flex-wrap items-center gap-3">
				<button
					on:click={sendTestPing}
					disabled={!canTest}
					class="btn-secondary text-sm disabled:opacity-50 disabled:cursor-not-allowed"
				>
					{testing ? 'Sending...' : 'Send test ping'}
				</button>
				{#if !heartbeatUrl.trim()}
					<span class="text-xs text-text-muted">Enter a URL to send a test ping.</span>
				{:else if isDirty}
					<span class="text-xs text-text-muted">Tests the URL above; remember to save it.</span>
				{/if}
			</div>

			<div aria-live="polite">
				{#if testResult}
					<div
						class="rounded-md border px-3 py-2 text-sm {testResult.ok ? 'border-success-muted bg-success-bg text-success' : 'border-danger-muted bg-danger-bg text-danger'}"
						data-testid="heartbeat-test-result"
					>
						{describeHeartbeatResult(testResult)}
					</div>
				{:else if testError}
					<div class="rounded-md border border-danger-muted bg-danger-bg px-3 py-2 text-sm text-danger" data-testid="heartbeat-test-result">
						Ping failed — {testError}
					</div>
				{/if}
			</div>
		</SectionCard>

		<SectionCard
			title="Overdue backups"
			description="When a job goes too long without a good backup, the dashboard turns red, a “Backups overdue” notification is sent, and the heartbeat stops."
		>
			<div class="max-w-xs">
				<FormInput
					id="stale-backup-hours"
					label="Overdue after (hours)"
					type="number"
					bind:value={staleHours}
					error={hoursError}
					mono={true}
					hint="0 = automatic from each job's schedule"
				/>
			</div>
			<p class="text-xs text-text-muted">
				Automatic allows each job its longest gap between scheduled runs plus a grace period (the larger of 2 hours or 25%). A fixed number applies the same window to every job.
			</p>
		</SectionCard>

		<SectionCard
			title="Shrinking database dumps"
			description="A database that suddenly dumps much smaller than usual has usually lost data: an app that re-created an empty database, a container pointed at the wrong database, a migration gone wrong. The run is marked incomplete and a “Backup incomplete” notification is sent."
		>
			<div class="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-xl">
				<FormInput
					id="dump-shrink-alert-percent"
					label="Flag a dump this much smaller (%)"
					type="number"
					bind:value={shrinkPercent}
					error={shrinkPercentError}
					mono={true}
					hint="0 turns the check off"
				/>
				<FormInput
					id="dump-shrink-min-mb"
					label="Ignore databases smaller than (MB)"
					type="number"
					bind:value={shrinkMinMb}
					error={shrinkMinError}
					mono={true}
					hint="0 checks every database"
				/>
			</div>
			<p class="text-xs text-text-muted">
				Each dump is compared with the median of that database's previous 5 successful dumps in the same format (at least 3 are needed). Real databases rarely lose half their data between two backups, so the default of 50% almost only fires when something is wrong. If the data was removed on purpose, accept the new size on the Databases page.
			</p>
		</SectionCard>

		<div class="flex items-center gap-3">
			<button on:click={save} disabled={saving || !!formError} class="btn-primary disabled:opacity-50">
				{#if saving}Saving...{:else}Save Settings{/if}
			</button>
			{#if saved}
				<span class="text-sm text-success">Settings saved</span>
			{/if}
			{#if isDirty}
				<span class="flex items-center gap-1.5 text-xs text-warning">
					<span class="w-1.5 h-1.5 rounded-full bg-warning"></span>
					Unsaved changes
				</span>
			{/if}
		</div>
	{/if}
</div>
