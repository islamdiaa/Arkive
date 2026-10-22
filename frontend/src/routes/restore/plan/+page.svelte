<svelte:head>
	<title>Restore Plan | Arkive</title>
</svelte:head>

<script lang="ts">
	import Header from '$lib/components/layout/Header.svelte';
	import { downloadRestorePlan } from '$lib/api/restore';
	import { addToast } from '$lib/stores/app';

	async function downloadPdf() {
		try {
			const { blob, filename, isPdf } = await downloadRestorePlan();
			const url = URL.createObjectURL(blob);
			const link = document.createElement('a');
			link.href = url;
			link.download = filename;
			document.body.appendChild(link);
			link.click();
			link.remove();
			URL.revokeObjectURL(url);
			addToast(
				isPdf
					? { type: 'info', message: 'Restore plan PDF downloaded.' }
					: { type: 'warning', message: `PDF generation is unavailable on the server; downloaded ${filename} instead.` },
			);
		} catch (e: any) {
			addToast({ type: 'error', message: e?.message || 'Failed to download restore plan' });
		}
	}
</script>

<Header title="Restore Plan" />

<main class="p-6 space-y-6">
	<div class="card">
		<div class="flex items-start gap-4">
			<div class="w-12 h-12 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
				<svg class="w-6 h-6 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" /></svg>
			</div>
			<div>
				<h2 class="text-lg font-semibold text-text">Disaster Recovery Plan</h2>
				<p class="text-sm text-text-secondary mt-1">
					Generate a comprehensive PDF document containing everything needed to restore your server from scratch.
					This includes storage target details, database restore commands, container inventory, and step-by-step instructions.
				</p>
			</div>
		</div>
	</div>

	<div class="grid grid-cols-1 md:grid-cols-3 gap-4">
		<div class="card">
			<h3 class="text-sm font-medium text-text mb-2">What's Included</h3>
			<ul class="space-y-2 text-sm text-text-secondary">
				<li class="flex items-center gap-2">
					<svg class="w-4 h-4 text-success shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					Storage target configuration
				</li>
				<li class="flex items-center gap-2">
					<svg class="w-4 h-4 text-success shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					Database restore commands
				</li>
				<li class="flex items-center gap-2">
					<svg class="w-4 h-4 text-success shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					Container inventory
				</li>
				<li class="flex items-center gap-2">
					<svg class="w-4 h-4 text-success shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					Step-by-step restore procedure
				</li>
				<li class="flex items-center gap-2">
					<svg class="w-4 h-4 text-success shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
					Flash config backup (Unraid)
				</li>
			</ul>
		</div>

		<div class="card">
			<h3 class="text-sm font-medium text-text mb-2">Best Practices</h3>
			<ul class="space-y-2 text-sm text-text-secondary">
				<li>Print and store in a safe location</li>
				<li>Update after major config changes</li>
				<li>Store encryption password separately</li>
				<li>Test restore procedure periodically</li>
				<li>Keep a copy offsite</li>
			</ul>
		</div>

		<div class="card flex flex-col items-center justify-center text-center">
			<svg class="w-16 h-16 text-primary/30 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" /></svg>
			<button on:click={downloadPdf} class="btn-primary">
				Download PDF
			</button>
			<p class="text-xs text-text-secondary mt-2">Generated on-demand with latest data</p>
		</div>
	</div>
</main>
