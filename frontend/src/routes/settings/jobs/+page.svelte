<svelte:head>
	<title>Backup Jobs | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { onMount } from 'svelte';
	import FormToggle from '$lib/components/ui/FormToggle.svelte';
	import ConfirmDangerModal from '$lib/components/ui/ConfirmDangerModal.svelte';
	import StatusBadge from '$lib/components/shared/StatusBadge.svelte';
	import { describeCron } from '$lib/utils/schedule';
	import {
		JOB_TYPES,
		JOB_TYPE_LABELS,
		JOB_TYPES_OVERVIEW,
		jobTypeDescription,
		jobTypeLabel,
	} from '$lib/utils/job-types';

	let jobs: any[] = [];
	let loading = true;
	let error = '';
	let showAdd = false;
	let runningJob = '';

	let newName = '';
	let newType = 'full';
	let newSchedule = '0 2 * * *';

	let deleteConfirmId = '';
	let deleteConfirmOpen = false;
	let deleteConfirmName = '';

	onMount(async () => {
		try {
			const result = await api.listJobs();
			jobs = result.items || result.jobs || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load backup jobs';
		}
		loading = false;
	});

	async function addJob() {
		try {
			await api.createJob({ name: newName, type: newType, schedule: newSchedule, targets: [], directories: [], exclude_patterns: [] });
			addToast({ type: 'success', message: `Job "${newName}" created` });
			showAdd = false;
			newName = '';
			const result = await api.listJobs();
			jobs = result.items || result.jobs || [];
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}

	async function toggleJob(job: any, nextValue: boolean) {
		const prev = job.enabled;
		job.enabled = nextValue;
		jobs = jobs;
		try {
			await api.updateJob(job.id, { enabled: nextValue });
			addToast({ type: 'success', message: `Job ${job.enabled ? 'enabled' : 'disabled'}` });
		} catch (e: any) {
			job.enabled = prev;
			jobs = jobs;
			addToast({ type: 'error', message: e.message });
		}
	}

	async function runNow(job: any) {
		runningJob = job.id;
		try {
			await api.triggerJob(job.id);
			addToast({ type: 'success', message: `Job "${job.name}" triggered` });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
		runningJob = '';
	}

	function openDeleteConfirm(job: any) {
		deleteConfirmId = job.id;
		deleteConfirmName = job.name;
		deleteConfirmOpen = true;
	}

	async function confirmDeleteJob() {
		try {
			await api.deleteJob(deleteConfirmId);
			jobs = jobs.filter(j => j.id !== deleteConfirmId);
			deleteConfirmId = '';
			deleteConfirmOpen = false;
			addToast({ type: 'success', message: 'Job deleted' });
		} catch (e: any) {
			addToast({ type: 'error', message: e.message });
		}
	}
</script>

<Header title="Backup Jobs" />

<ConfirmDangerModal
	bind:open={deleteConfirmOpen}
	title="Delete Backup Job"
	message="Are you sure you want to delete &quot;{deleteConfirmName}&quot;? This cannot be undone."
	confirmText="DELETE"
	confirmLabel="Delete Job"
	on:confirm={confirmDeleteJob}
	on:cancel={() => { deleteConfirmOpen = false; deleteConfirmId = ''; }}
/>

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	<div class="flex items-center justify-between">
		<p class="text-sm text-text-secondary">{jobs.length} backup job{jobs.length !== 1 ? 's' : ''}</p>
		<button on:click={() => showAdd = !showAdd} class="btn-primary text-sm">{showAdd ? 'Cancel' : 'Add Job'}</button>
	</div>

	{#if showAdd}
		<div class="card">
			<h3 class="font-semibold text-text mb-4">Create Backup Job</h3>
			<div class="grid grid-cols-1 md:grid-cols-3 gap-4">
				<div>
					<label for="new-job-name" class="block text-sm text-text-secondary mb-1">Name</label>
					<input id="new-job-name" type="text" bind:value={newName} class="input" placeholder="Nightly Full" />
				</div>
				<div>
					<label for="new-job-type" class="block text-sm text-text-secondary mb-1">Type</label>
					<select id="new-job-type" bind:value={newType} class="input" aria-describedby="new-job-type-help">
						{#each JOB_TYPES as t}
							<option value={t}>{JOB_TYPE_LABELS[t]}</option>
						{/each}
					</select>
				</div>
				<div>
					<label for="new-job-schedule" class="block text-sm text-text-secondary mb-1">Schedule (cron)</label>
					<input id="new-job-schedule" type="text" bind:value={newSchedule} class="input font-mono" />
				</div>
			</div>
			<div id="new-job-type-help" class="mt-3 rounded-md border border-border-muted bg-bg-elevated/50 px-3 py-2 text-xs text-text-secondary space-y-1">
				<p><span class="font-medium text-text">{jobTypeLabel(newType)}:</span> {jobTypeDescription(newType)}</p>
				<p>{JOB_TYPES_OVERVIEW}</p>
			</div>
			<button on:click={addJob} disabled={!newName} class="btn-primary mt-4 disabled:opacity-50">Create</button>
		</div>
	{/if}

	{#if loading}
		<div class="space-y-4">
			{#each Array(3) as _}
				<div class="card animate-skeleton h-20"></div>
			{/each}
		</div>
	{:else if jobs.length === 0}
		<div class="card text-center py-12">
			<svg class="w-12 h-12 text-text-secondary mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" /></svg>
			<p class="text-text-secondary">No backup jobs configured yet.</p>
			<button on:click={() => showAdd = true} class="btn-primary mt-4">Create Your First Job</button>
		</div>
	{:else}
		<div class="space-y-3">
			{#each jobs as job}
				<div class="card {job.enabled ? '' : 'opacity-60'}">
					<div class="flex items-center justify-between">
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 mb-1">
								<p class="text-sm font-medium text-text">{job.name}</p>
								{#if !job.enabled}
									<span class="text-xs text-text-secondary">Paused</span>
								{/if}
								{#if job.last_run?.status}
									<span class="text-xs text-text-secondary">Last run:</span>
									<StatusBadge status={job.last_run.status} size="sm" />
								{/if}
							</div>
							<p class="text-xs text-text-secondary">
								<span title={jobTypeDescription(job.type)}>{jobTypeLabel(job.type)}</span>
								&middot;
								<code class="font-mono">{job.schedule}</code>
							</p>
							<p class="text-xs text-text-secondary mt-0.5">{describeCron(job.schedule)}</p>
						</div>
						<div class="flex items-center gap-3">
							<FormToggle
								checked={job.enabled}
								on:change={(e) => toggleJob(job, e.detail.checked)}
							/>
							<button
								on:click={() => runNow(job)}
								disabled={runningJob === job.id}
								class="btn-secondary text-xs disabled:opacity-50"
							>
								{runningJob === job.id ? 'Running...' : 'Run Now'}
							</button>
							<button on:click={() => openDeleteConfirm(job)} class="text-text-secondary hover:text-danger" aria-label="Delete job">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
							</button>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</main>
