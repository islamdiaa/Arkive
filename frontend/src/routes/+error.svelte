<script lang="ts">
  import { page } from '$lib/stores/page';
  import { Button } from '$lib/components/ui/button';
</script>

<svelte:head>
  <title>Error {$page.status} - Arkive</title>
</svelte:head>

<div class="flex items-center justify-center min-h-[70vh]">
  <div class="flex flex-col items-center text-center max-w-md px-6">
    <!-- Error code -->
    <div class="text-6xl font-bold font-mono text-text-muted mb-2">
      {$page.status}
    </div>

    <!-- Error message -->
    <h1 class="text-xl font-semibold text-text-primary mb-2">
      {#if $page.status === 404}
        Page Not Found
      {:else if $page.status === 500}
        Server Error
      {:else if $page.status === 403}
        Access Denied
      {:else}
        Something Went Wrong
      {/if}
    </h1>

    <p class="text-sm text-text-secondary mb-6">
      {#if $page.error?.message}
        {$page.error.message}
      {:else if $page.status === 404}
        The page you are looking for does not exist or has been moved.
      {:else if $page.status === 500}
        An unexpected error occurred. Please try again or check the logs.
      {:else if $page.status === 403}
        You do not have permission to access this resource.
      {:else}
        An unexpected error occurred.
      {/if}
    </p>

    <!-- Actions -->
    <div class="flex items-center gap-3">
      <Button href="/" variant="default">
        Go Home
      </Button>
      <Button variant="outline" on:click={() => window.location.reload()}>
        Reload Page
      </Button>
    </div>

    <!-- Error details (dev) -->
    {#if $page.error?.message && $page.status >= 500}
      <details class="mt-8 w-full text-left">
        <summary class="text-xs text-text-muted cursor-pointer hover:text-text-secondary transition-colors">
          Technical Details
        </summary>
        <pre class="mt-2 p-3 bg-bg-surface border border-border rounded-md text-xs text-text-secondary font-mono overflow-x-auto whitespace-pre-wrap">
{$page.error.message}
        </pre>
      </details>
    {/if}
  </div>
</div>
