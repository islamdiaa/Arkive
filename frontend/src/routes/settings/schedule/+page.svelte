<svelte:head>
	<title>Schedule | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { api } from '$lib/api/client';
	import { addToast } from '$lib/stores/app';
	import { onMount } from 'svelte';
	import FormToggle from '$lib/components/ui/FormToggle.svelte';
	import SectionCard from '$lib/components/ui/SectionCard.svelte';
	import { jobTypeDescription, jobTypeLabel } from '$lib/utils/job-types';

	let jobs: any[] = [];
	let loading = true;
	let error = '';

	onMount(async () => {
		try {
			const result = await api.listJobs();
			jobs = result.items || result.jobs || [];
		} catch (e: any) {
			console.error(e);
			error = e.message || 'Failed to load schedules';
		}
		loading = false;
	});

	async function updateSchedule(job: any) {
		try {
			await api.updateJob(job.id, { schedule: job.schedule });
			addToast({ type: 'success', message: `Schedule updated for ${job.name}` });
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
			addToast({ type: 'info', message: `${job.name} ${job.enabled ? 'enabled' : 'paused'}` });
		} catch (e: any) {
			job.enabled = prev;
			jobs = jobs;
			addToast({ type: 'error', message: e.message });
		}
	}
</script>

<Header title="Schedule" />

<main class="p-6 space-y-6">
	{#if error}
		<div class="p-4 bg-danger-bg border border-danger/30 rounded text-danger text-sm">{error}</div>
	{/if}

	{#if loading}
		<div class="space-y-4">
			{#each Array(3) as _}
				<div class="card animate-skeleton h-24"></div>
			{/each}
		</div>
	{:else if jobs.length === 0}
		<div class="card text-center py-12">
			<p class="text-text-secondary">No backup jobs configured. Create a job first to set up schedules.</p>
			<a href="/settings/jobs" class="btn-primary mt-4 inline-block">Go to Backup Jobs</a>
		</div>
	{:else}
		{#each jobs as job}
			<SectionCard title={job.name} description="{jobTypeLabel(job.type)} — {jobTypeDescription(job.type)}">
				<FormToggle
					checked={job.enabled}
					label={job.enabled ? 'Enabled' : 'Paused'}
					description="Toggle to enable or pause this job's schedule"
					on:change={(e) => toggleJob(job, e.detail.checked)}
				/>
				<div>
					<label for="schedule-{job.id}" class="block text-xs text-text-secondary mb-1">Cron Schedule</label>
					<div class="flex items-end gap-3">
						<input
							id="schedule-{job.id}"
							type="text"
							bind:value={job.schedule}
							class="input font-mono text-sm flex-1"
						/>
						<button on:click={() => updateSchedule(job)} class="btn-secondary text-sm">Save</button>
					</div>
				</div>
				{#if job.next_run}
					<div>
						<p class="text-xs text-text-secondary mb-0.5">Next run</p>
						<p class="text-xs font-mono text-text">{job.next_run}</p>
					</div>
				{/if}
			</SectionCard>
		{/each}
	{/if}
</main>
