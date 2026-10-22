<script lang="ts">
	import { KeyRound } from 'lucide-svelte';

	/** Show only when the status payload explicitly says the kit is unconfirmed. */
	export let confirmed: boolean | null | undefined = undefined;
</script>

{#if confirmed === false}
	<a
		href="/settings/recovery-kit"
		class="group block rounded-lg border border-warning bg-warning-bg px-4 py-3 hover:bg-warning-muted transition-colors"
		data-testid="recovery-kit-banner"
	>
		<div class="flex items-start gap-3">
			<KeyRound class="w-5 h-5 text-warning shrink-0 mt-0.5" />
			<div class="min-w-0 flex-1">
				<div class="flex items-center justify-between gap-3">
					<p class="text-sm font-semibold text-warning">Save your recovery kit</p>
					<span class="text-xs text-warning whitespace-nowrap">Recovery kit &rarr;</span>
				</div>
				<p class="mt-1 text-xs text-text-secondary">
					Save your recovery kit — without the encryption password stored somewhere other than this server, your backups can't be restored if this server dies.
				</p>
			</div>
		</div>
	</a>
{/if}
