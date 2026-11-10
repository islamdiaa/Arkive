<script lang="ts">
	import { ExternalLink, Github, Globe } from 'lucide-svelte';
	import { systemStatus } from '$lib/stores/app';
</script>

<footer class="mt-8 border-t border-border px-6 py-4 text-xs text-text-tertiary">
	<div class="flex flex-col items-center justify-between gap-2 sm:flex-row">
		<span>
			Arkive{$systemStatus?.version ? ` v${$systemStatus.version}` : ''} · open-source backup for Unraid, by
			Islam Diaa
		</span>
		<nav aria-label="Project links" class="flex items-center gap-4">
			<a
				class="inline-flex items-center gap-1.5 hover:text-text transition-colors"
				href="https://itayar.com"
				target="_blank"
				rel="noopener noreferrer"
			>
				<Globe size={13} aria-hidden="true" />
				itayar.com
			</a>
			<a
				class="inline-flex items-center gap-1.5 hover:text-text transition-colors"
				href="https://github.com/islamdiaa"
				target="_blank"
				rel="noopener noreferrer"
			>
				<Github size={13} aria-hidden="true" />
				GitHub
			</a>
			<a
				class="inline-flex items-center gap-1.5 hover:text-text transition-colors"
				href="https://github.com/islamdiaa/Arkive"
				target="_blank"
				rel="noopener noreferrer"
			>
				<ExternalLink size={13} aria-hidden="true" />
				Source
			</a>
		</nav>
	</div>
</footer>
