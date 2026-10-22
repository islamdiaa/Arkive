<script lang="ts">
	import { connected, reconnectAttempts } from '$lib/stores/sse';
	import { setupCompleted } from '$lib/stores/app';
	import { authenticated } from '$lib/stores/auth';
</script>

{#if $setupCompleted && $authenticated && !$connected}
<div class="bg-warning/10 border-b border-warning/20 px-4 py-2 text-center text-sm text-warning">
	Connection lost — reconnecting{$reconnectAttempts > 0 ? ` (attempt ${$reconnectAttempts})` : ''}...
</div>
{/if}
