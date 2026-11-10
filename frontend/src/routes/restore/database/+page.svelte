<svelte:head>
	<title>Restore a Database | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import {
		confirmationMatches,
		defaultStopSelection,
		getRestore,
		isFinished,
		listRestoreOptions,
		listRestores,
		loadPercent,
		methodLabel,
		optionKey,
		previewRestore,
		startRestore,
		statusBadgeClass,
		statusLabel,
		undoRestore,
		type GuidedRestore,
		type RestoreOption,
		type RestorePreview,
		type RestoreSelection,
	} from '$lib/api/guided-restore';
	import { addToast } from '$lib/stores/app';
	import { copyText } from '$lib/utils/download';
	import { formatBytes, formatDuration } from '$lib/utils/format';
	import { formatDate, timeAgo } from '$lib/utils/date';
	import type { Snapshot } from '$lib/types';
	import { page } from '$lib/stores/page';
	import { goto } from '$app/navigation';
	import { onDestroy, onMount } from 'svelte';
	import { AlertTriangle, CheckCircle2, Circle, Copy, Database, Loader2, MinusCircle, RotateCcw, XCircle } from 'lucide-svelte';

	const POLL_MS = 2000;

	let options: RestoreOption[] = [];
	let optionsLoading = true;
	let loadError = '';
	let snapshots: Snapshot[] = [];
	let targetNames: Record<string, string> = {};

	let selected: RestoreOption | null = null;
	let snapshotKey = '';
	let preview: RestorePreview | null = null;
	let previewing = false;
	let previewError = '';
	let stopSelection: string[] = [];
	let typed = '';
	let starting = false;

	let op: GuidedRestore | null = null;
	let history: GuidedRestore[] = [];
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;

	let undoOpen = false;
	let undoTyped = '';
	let undoing = false;

	$: sortedSnapshots = newestFirst(snapshots);
	$: chosenSnapshot = sortedSnapshots.find((s) => snapKey(s) === snapshotKey) ?? null;
	$: confirmed = preview?.method === 'manual' || confirmationMatches(typed, preview?.confirm_phrase);
	$: canStart = !!preview && preview.blockers.length === 0 && confirmed && !starting;
	$: percent = op ? loadPercent(op) : null;

	function newestFirst(list: Snapshot[]): Snapshot[] {
		return [...list].sort((a, b) => (b.time || '').localeCompare(a.time || ''));
	}

	function snapKey(s: Snapshot): string {
		return `${s.target_id}:${s.id}`;
	}

	function snapshotLabel(s: Snapshot): string {
		const when = s.time ? `${formatDate(s.time)} (${timeAgo(s.time)})` : 'unknown time';
		return `${when} · ${targetNames[s.target_id] || s.target_id} · ${(s.short_id || s.id).slice(0, 8)}`;
	}

	function selection(): RestoreSelection | null {
		if (!selected || !chosenSnapshot) return null;
		return {
			container_name: selected.container_name,
			db_type: selected.db_type,
			db_name: selected.db_name,
			host_path: selected.host_path,
			snapshot_id: chosenSnapshot.id,
			target_id: chosenSnapshot.target_id,
		};
	}

	function resetPreview() {
		preview = null;
		previewError = '';
		typed = '';
		stopSelection = [];
	}

	function choose(option: RestoreOption) {
		if (option.method === 'unsupported') return;
		selected = option;
		resetPreview();
	}

	function chooseSnapshot(key: string) {
		snapshotKey = key;
		resetPreview();
	}

	async function runPreview() {
		const sel = selection();
		if (!sel) return;
		previewing = true;
		resetPreview();
		try {
			preview = await previewRestore(sel);
			stopSelection = defaultStopSelection(preview.dependents);
		} catch (e: any) {
			previewError = e?.message || 'Could not check this snapshot';
		}
		previewing = false;
	}

	function toggleStop(name: string, checked: boolean) {
		stopSelection = checked ? [...new Set([...stopSelection, name])] : stopSelection.filter((n) => n !== name);
	}

	async function start() {
		const sel = selection();
		if (!sel || !canStart) return;
		starting = true;
		try {
			const result = await startRestore(sel, preview?.method === 'load' ? typed : '', preview?.method === 'load' ? stopSelection : []);
			await showOperation(result.id);
			goto(`/restore/database?id=${result.id}`, { replace: true, reset: false });
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Could not start the restore' });
		}
		starting = false;
	}

	async function showOperation(id: string) {
		stopPolling();
		undoOpen = false;
		undoTyped = '';
		try {
			op = await getRestore(id);
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Restore not found' });
			op = null;
			return;
		}
		if (!isFinished(op.status)) schedulePoll(id);
	}

	function schedulePoll(id: string) {
		if (destroyed) return;
		pollTimer = setTimeout(async () => {
			try {
				op = await getRestore(id);
			} catch {
				/* transient: keep polling */
			}
			if (op && !isFinished(op.status)) {
				schedulePoll(id);
			} else {
				await refreshHistory();
			}
		}, POLL_MS);
	}

	function stopPolling() {
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = null;
	}

	async function refreshHistory() {
		try {
			history = (await listRestores(10)).items;
		} catch {
			/* history is secondary */
		}
	}

	async function startUndo() {
		if (!op || !confirmationMatches(undoTyped, op.db_name)) return;
		undoing = true;
		try {
			const result = await undoRestore(op.id, undoTyped);
			await showOperation(result.id);
			goto(`/restore/database?id=${result.id}`, { replace: true, reset: false });
			await refreshHistory();
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Could not start the undo' });
		}
		undoing = false;
	}

	function newRestore() {
		stopPolling();
		op = null;
		resetPreview();
		goto('/restore/database', { replace: true, reset: false });
	}

	async function copy(command: string) {
		const ok = await copyText(command);
		addToast(ok ? { type: 'success', message: 'Copied' } : { type: 'error', message: 'Copy failed' });
	}

	onMount(async () => {
		const params = $page.url.searchParams;
		const [optionsResult, snapshotsResult, targetsResult] = await Promise.allSettled([
			listRestoreOptions(),
			api.listSnapshots(),
			api.listTargets(),
		]);
		if (optionsResult.status === 'fulfilled') {
			options = optionsResult.value;
		} else {
			loadError = optionsResult.reason?.message || 'Failed to load databases';
		}
		if (snapshotsResult.status === 'fulfilled') {
			const value: any = snapshotsResult.value;
			snapshots = value?.snapshots || value?.items || [];
		}
		if (targetsResult.status === 'fulfilled') {
			const value: any = targetsResult.value;
			for (const t of value?.targets || value?.items || []) targetNames[t.id] = t.name;
			targetNames = targetNames;
		}
		optionsLoading = false;

		const wanted = options.find(
			(o) =>
				o.container_name === params.get('container') &&
				o.db_name === params.get('db') &&
				(!params.get('type') || o.db_type === params.get('type')),
		);
		if (wanted) choose(wanted);
		const newest = newestFirst(snapshots)[0];
		if (newest) snapshotKey = snapKey(newest);

		const id = params.get('id');
		if (id) await showOperation(id);
		await refreshHistory();
	});

	onDestroy(() => {
		destroyed = true;
		stopPolling();
	});
</script>

<Header title="Restore a Database" />

<main class="p-6 space-y-6 max-w-5xl">
	<div>
		<h2 class="text-2xl font-semibold text-text">Restore a database</h2>
		<p class="text-sm text-text-secondary mt-1">
			Put one database back the way it was in a snapshot. Arkive takes a safety dump of the current database first, so the
			restore can be undone. To restore files instead, use <a href="/restore/browse" class="btn-link">Browse &amp; Restore</a>.
		</p>
	</div>

	{#if loadError}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{loadError}</div>
	{/if}

	{#if op}
		<!-- ── Operation progress / result ── -->
		<section class="card space-y-5" aria-live="polite" data-testid="guided-restore-operation">
			<div class="flex flex-wrap items-start justify-between gap-3">
				<div>
					<h2 class="text-lg font-semibold text-text">
						{op.kind === 'undo' ? 'Undo of a restore' : 'Restore'}: {op.container_name}/{op.db_name}
					</h2>
					<p class="text-sm text-text-secondary mt-1">
						{#if op.kind === 'undo'}
							Loading the safety dump taken before the restore
						{:else}
							From snapshot <span class="font-mono">{op.snapshot_id}</span>{op.target_name ? ` on ${op.target_name}` : ''}
						{/if}
						{` · started ${formatDate(op.started_at)}`}{op.duration_seconds != null ? ` · took ${formatDuration(op.duration_seconds)}` : ''}
					</p>
				</div>
				<span class={statusBadgeClass(op.status)} data-testid="guided-restore-status">{statusLabel(op.status)}</span>
			</div>

			<ol class="space-y-2" data-testid="guided-restore-steps">
				{#each op.steps as step (step.key)}
					<li class="flex items-start gap-3 text-sm">
						<span class="mt-0.5 shrink-0" aria-hidden="true">
							{#if step.status === 'done'}
								<CheckCircle2 class="w-4 h-4 text-success" />
							{:else if step.status === 'running'}
								<Loader2 class="w-4 h-4 text-primary animate-spin" />
							{:else if step.status === 'failed'}
								<XCircle class="w-4 h-4 text-danger" />
							{:else if step.status === 'skipped'}
								<MinusCircle class="w-4 h-4 text-text-muted" />
							{:else}
								<Circle class="w-4 h-4 text-text-muted" />
							{/if}
						</span>
						<div class="min-w-0">
							<p class={step.status === 'pending' || step.status === 'skipped' ? 'text-text-secondary' : 'text-text'}>
								{step.label}<span class="sr-only"> ({step.status})</span>
							</p>
							{#if step.detail}
								<p class="text-xs text-text-secondary break-words">{step.detail}</p>
							{/if}
							{#if step.key === 'load' && step.status === 'running' && percent !== null && op.progress}
								<div class="mt-2 w-64 max-w-full">
									<div class="h-1.5 rounded bg-surface-hover overflow-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent}>
										<div class="h-full bg-primary transition-all" style="width: {percent}%"></div>
									</div>
									<p class="text-xs text-text-muted mt-1">
										{formatBytes(op.progress.bytes_done)} of {formatBytes(op.progress.bytes_total)} sent ({percent}%)
									</p>
								</div>
							{/if}
						</div>
					</li>
				{/each}
			</ol>

			{#if op.error_message}
				<div class="flex items-start gap-2 p-3 bg-danger-bg border border-danger/30 rounded text-danger text-sm" role="alert">
					<XCircle class="w-4 h-4 shrink-0 mt-0.5" />
					<p class="break-words">{op.error_message}</p>
				</div>
			{/if}

			{#if op.warnings.length}
				<div class="p-3 bg-warning-bg border border-warning/30 rounded text-warning text-sm space-y-1">
					{#each op.warnings as warning}
						<p class="flex items-start gap-2"><AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />{warning}</p>
					{/each}
				</div>
			{/if}

			{#if op.status === 'action_required' && op.instructions.length}
				<div class="space-y-3" data-testid="guided-restore-instructions">
					<p class="text-sm text-text">
						Arkive never writes to your shares, so <span class="font-mono break-all">{op.host_path}</span> is swapped by hand.
						The restored database is ready; run these steps on the server to put it in place:
					</p>
					<ol class="space-y-3 list-decimal pl-5 text-sm text-text-secondary">
						{#each op.instructions as step}
							<li class="space-y-1">
								<p>{step.text}</p>
								{#if step.command}
									<div class="flex items-start gap-2">
										<code class="flex-1 min-w-0 rounded-md border border-border bg-bg-input px-3 py-2 text-xs font-mono text-text break-all">{step.command}</code>
										<button type="button" class="btn-ghost btn-sm flex items-center gap-1.5" on:click={() => copy(step.command || '')} aria-label="Copy command">
											<Copy class="w-3.5 h-3.5" />
										</button>
									</div>
								{/if}
							</li>
						{/each}
					</ol>
				</div>
			{/if}

			{#if op.safety_dump && op.method === 'load'}
				<p class="text-xs text-text-secondary">
					Safety dump: <span class="font-mono break-all">{op.safety_dump.path}</span> ({formatBytes(op.safety_dump.size_bytes)}){#if !op.safety_dump.available} — no longer available{/if}
				</p>
			{/if}

			{#if isFinished(op.status)}
				<div class="flex flex-wrap items-center gap-3 pt-1">
					{#if op.can_undo}
						<button class="btn-danger text-sm flex items-center gap-1.5" on:click={() => { undoOpen = !undoOpen; undoTyped = ''; }} aria-expanded={undoOpen}>
							<RotateCcw class="w-4 h-4" /> Undo this {op.kind === 'undo' ? 'undo' : 'restore'}
						</button>
					{/if}
					<button class="btn-secondary text-sm" on:click={newRestore}>Restore another database</button>
				</div>

				{#if undoOpen && op.can_undo}
					<div class="rounded-lg border border-danger/40 p-4 space-y-3" data-testid="guided-restore-undo">
						<p class="text-sm text-text">
							Undo loads the safety dump taken just before this {op.kind}, replacing what is in
							<span class="font-mono">{op.db_name}</span> now. A new safety dump is taken first.
							{#if op.stopped_containers.length || op.stop_containers.length}
								These apps are stopped during the undo and started again afterwards:
								<span class="font-mono">{[...new Set([...op.stop_containers, ...op.stopped_containers])].join(', ')}</span>.
							{/if}
						</p>
						<label class="block text-xs font-semibold uppercase tracking-wide text-text-secondary" for="undo-confirm">
							Type <span class="font-mono normal-case text-text">{op.db_name}</span> to confirm
						</label>
						<div class="flex flex-wrap gap-2">
							<input id="undo-confirm" class="input font-mono max-w-sm" autocomplete="off" spellcheck="false" bind:value={undoTyped} placeholder={op.db_name} />
							<button class="btn-danger-solid text-sm disabled:opacity-50" disabled={!confirmationMatches(undoTyped, op.db_name) || undoing} on:click={startUndo}>
								{undoing ? 'Starting…' : 'Undo now'}
							</button>
						</div>
					</div>
				{/if}
			{/if}
		</section>
	{:else}
		<!-- ── 1. Database ── -->
		<section class="card space-y-4">
			<h2 class="text-base font-semibold text-text">1. Choose a database</h2>
			{#if optionsLoading}
				<div class="h-24 animate-skeleton rounded bg-surface-hover"></div>
			{:else if options.length === 0}
				<p class="text-sm text-text-secondary">
					No databases discovered yet. Run a scan on the <a href="/databases" class="btn-link">Databases</a> page.
				</p>
			{:else}
				<div class="grid grid-cols-1 md:grid-cols-2 gap-2" role="radiogroup" aria-label="Database">
					{#each options as option (optionKey(option))}
						{@const isSelected = !!selected && optionKey(selected) === optionKey(option)}
						<button
							type="button"
							role="radio"
							aria-checked={isSelected}
							disabled={option.method === 'unsupported'}
							title={option.reason || ''}
							on:click={() => choose(option)}
							class="text-left rounded-lg border p-3 transition-colors disabled:opacity-50 disabled:cursor-not-allowed
								{isSelected ? 'border-primary bg-primary/5' : 'border-border hover:border-border-strong'}"
						>
							<div class="flex items-center gap-2">
								<Database class="w-4 h-4 text-text-secondary shrink-0" />
								<span class="text-sm font-medium text-text truncate">{option.db_name}</span>
								<span class="badge bg-neutral-bg text-neutral">{option.db_type}</span>
							</div>
							<p class="text-xs text-text-secondary mt-1 truncate">
								{option.container_name}{#if option.container_status && option.container_status !== 'running'} ({option.container_status}){/if} · {methodLabel(option.method)}
							</p>
							{#if option.method === 'unsupported' && option.reason}
								<p class="text-xs text-text-muted mt-1">{option.reason}</p>
							{/if}
						</button>
					{/each}
				</div>
			{/if}
		</section>

		<!-- ── 2. Snapshot ── -->
		<section class="card space-y-4">
			<h2 class="text-base font-semibold text-text">2. Choose a restore point</h2>
			{#if sortedSnapshots.length === 0}
				<p class="text-sm text-text-secondary">No snapshots yet. Run a backup first.</p>
			{:else}
				<div class="flex flex-wrap items-end gap-3">
					<div class="flex-1 min-w-[16rem]">
						<label for="snapshot" class="block text-xs font-semibold uppercase tracking-wide text-text-secondary mb-1.5">Snapshot</label>
						<select id="snapshot" class="input" value={snapshotKey} on:change={(e) => chooseSnapshot(e.currentTarget.value)} disabled={!selected}>
							{#each sortedSnapshots as snap (snapKey(snap))}
								<option value={snapKey(snap)}>{snapshotLabel(snap)}</option>
							{/each}
						</select>
					</div>
					<button class="btn-primary text-sm disabled:opacity-50" disabled={!selected || !chosenSnapshot || previewing} on:click={runPreview}>
						{previewing ? 'Checking…' : 'Check snapshot'}
					</button>
				</div>
				<p class="text-xs text-text-muted">Checking finds the database's dump in the snapshot and checks the container. Nothing is changed.</p>
			{/if}
			{#if previewError}
				<div class="p-3 bg-danger-bg border border-danger/30 rounded text-danger text-sm" role="alert">{previewError}</div>
			{/if}
		</section>

		<!-- ── 3. Review & confirm ── -->
		{#if preview && selected}
			<section class="card space-y-5" data-testid="guided-restore-review">
				<h2 class="text-base font-semibold text-text">3. Review and confirm</h2>

				<dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
					<div><dt class="text-text-secondary">Database</dt><dd class="text-text font-mono break-all">{preview.database.container_name}/{preview.database.db_name}</dd></div>
					<div><dt class="text-text-secondary">Dump</dt><dd class="text-text"><span class="font-mono break-all">{preview.dump.file}</span> ({formatBytes(preview.dump.size_bytes)})</dd></div>
					<div><dt class="text-text-secondary">Snapshot</dt><dd class="text-text">{preview.snapshot.time ? formatDate(preview.snapshot.time) : preview.snapshot.id} on {preview.snapshot.target_name}</dd></div>
					{#if preview.database.container_detail}
						<div><dt class="text-text-secondary">Container</dt><dd class="text-text">{preview.database.container_detail}</dd></div>
					{/if}
				</dl>

				{#if preview.blockers.length}
					<div class="p-3 bg-danger-bg border border-danger/30 rounded text-danger text-sm space-y-1" role="alert">
						{#each preview.blockers as blocker}<p>{blocker}</p>{/each}
					</div>
				{/if}
				{#each preview.warnings as warning}
					<div class="flex items-start gap-2 p-3 bg-warning-bg border border-warning/30 rounded text-warning text-sm">
						<AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />{warning}
					</div>
				{/each}

				<div>
					<p class="text-sm font-medium text-text mb-2">What happens</p>
					<ol class="list-decimal pl-5 space-y-1 text-sm text-text-secondary">
						{#each preview.steps as step}<li>{step.label}</li>{/each}
					</ol>
				</div>

				{#if preview.method === 'load'}
					<p class="text-sm text-text-secondary">
						Everything in <span class="font-mono text-text">{preview.database.db_name}</span> is replaced with the snapshot's
						copy. The safety dump is kept for {preview.safety_dump_retention_days} days in Arkive's restores folder, and
						<span class="text-text">Undo</span> loads it back.
					</p>

					{#if preview.dependents.length}
						<fieldset class="space-y-2">
							<legend class="text-sm font-medium text-text mb-1">Stop these apps during the restore</legend>
							{#each preview.dependents as dep (dep.name)}
								<label class="flex items-start gap-2 text-sm">
									<input
										type="checkbox"
										class="mt-0.5"
										checked={stopSelection.includes(dep.name)}
										on:change={(e) => toggleStop(dep.name, e.currentTarget.checked)}
									/>
									<span>
										<span class="font-mono text-text">{dep.name}</span>
										<span class="text-text-secondary">({dep.status || 'unknown'}; {dep.reason})</span>
									</span>
								</label>
							{/each}
							<p class="text-xs text-text-muted">They are started again when the restore succeeds. If loading fails part-way they stay stopped until you undo.</p>
						</fieldset>
					{/if}

					<div class="rounded-lg border border-danger/40 p-4 space-y-2">
						<label for="restore-confirm" class="block text-xs font-semibold uppercase tracking-wide text-text-secondary">
							Type <span class="font-mono normal-case text-text">{preview.confirm_phrase}</span> to replace the current database
						</label>
						<input id="restore-confirm" class="input font-mono max-w-sm" autocomplete="off" spellcheck="false" bind:value={typed} placeholder={preview.confirm_phrase || ''} />
					</div>
				{:else}
					<p class="text-sm text-text-secondary">
						Arkive restores and checks the file and saves a copy of the current one. Because Arkive's access to your shares
						is read-only, you swap the file in yourself; the exact commands are shown next.
					</p>
				{/if}

				<div class="flex flex-wrap items-center gap-3">
					<button class="{preview.method === 'load' ? 'btn-danger-solid' : 'btn-primary'} text-sm disabled:opacity-50" disabled={!canStart} on:click={start}>
						{#if starting}Starting…{:else if preview.method === 'load'}Restore {preview.database.db_name}{:else}Prepare restored file{/if}
					</button>
					<button class="btn-ghost text-sm" on:click={resetPreview}>Cancel</button>
				</div>
			</section>
		{/if}
	{/if}

	<!-- ── History ── -->
	{#if history.length}
		<section class="card space-y-3">
			<h2 class="text-base font-semibold text-text">Recent database restores</h2>
			<ul class="divide-y divide-border">
				{#each history as item (item.id)}
					<li class="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
						<div class="min-w-0">
							<span class="text-text font-medium">{item.kind === 'undo' ? 'Undo' : 'Restore'}: {item.container_name}/{item.db_name}</span>
							<span class="text-text-secondary"> · {timeAgo(item.started_at)}</span>
						</div>
						<div class="flex items-center gap-3">
							<span class={statusBadgeClass(item.status)}>{statusLabel(item.status)}</span>
							<button class="btn-link text-xs" on:click={() => { showOperation(item.id); goto(`/restore/database?id=${item.id}`, { replace: true, reset: false }); }}>View</button>
						</div>
					</li>
				{/each}
			</ul>
		</section>
	{/if}
</main>
