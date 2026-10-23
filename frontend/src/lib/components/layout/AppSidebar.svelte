<script lang="ts">
  import { page } from '$app/stores';
  import { createEventDispatcher } from 'svelte';
  import {
    LayoutDashboard,
    HardDrive,
    Database,
    RotateCcw,
    Activity,
    FileText,
    Settings,
    ChevronRight,
    X,
  } from 'lucide-svelte';
  import { cn } from '$lib/utils/cn';

  export let open = false;
  export let collapsed = false;

  const dispatch = createEventDispatcher<{ close: void }>();

  const navItems = [
    { label: 'Dashboard', href: '/', icon: LayoutDashboard },
    { label: 'Backups', href: '/backups', icon: HardDrive },
    { label: 'Databases', href: '/databases', icon: Database },
    { label: 'Restore', href: '/restore', icon: RotateCcw },
    { label: 'Activity', href: '/activity', icon: Activity },
    { label: 'Logs', href: '/logs', icon: FileText },
  ];

  const settingsItems = [
    { label: 'General', href: '/settings' },
    { label: 'Targets', href: '/settings/targets' },
    { label: 'Schedule', href: '/settings/schedule' },
    { label: 'Jobs', href: '/settings/jobs' },
    { label: 'Directories', href: '/settings/directories' },
    { label: 'Notifications', href: '/settings/notifications' },
    { label: 'Monitoring', href: '/settings/monitoring' },
    { label: 'Security', href: '/settings/security' },
    { label: 'Recovery Kit', href: '/settings/recovery-kit' },
  ];

  let settingsExpanded = false;

  $: if ($page.url.pathname.startsWith('/settings')) {
    settingsExpanded = true;
  }

  $: isActive = (href: string) =>
    href === '/' ? $page.url.pathname === '/' : $page.url.pathname.startsWith(href);

  $: isSettingsActive = $page.url.pathname.startsWith('/settings');

  function handleOverlayClick() {
    dispatch('close');
  }

  function handleNavClick() {
    // Close mobile drawer on navigation
    if (open) {
      dispatch('close');
    }
  }

  function toggleSettings() {
    settingsExpanded = !settingsExpanded;
  }
</script>

<!-- Mobile overlay backdrop -->
{#if open}
  <div
    class="fixed inset-0 z-[29] bg-[var(--bg-overlay)] lg:hidden"
    on:click={handleOverlayClick}
    on:keydown={(e) => e.key === 'Escape' && handleOverlayClick()}
    role="button"
    tabindex="-1"
    aria-label="Close sidebar"
  ></div>
{/if}

<!-- Sidebar -->
<aside
  class={cn(
    'fixed top-0 left-0 z-[30] h-screen flex flex-col',
    'bg-bg-sidebar border-r border-border',
    'transition-all duration-300 ease-in-out',
    // Mobile: 280px drawer, slide in/out
    open ? 'translate-x-0' : '-translate-x-full',
    'w-[280px]',
    // Tablet: 56px collapsed (icons only)
    'md:translate-x-0',
    collapsed ? 'md:w-14' : 'md:w-[220px]',
    // Desktop: always 220px
    'lg:translate-x-0 lg:w-[220px]'
  )}
>
  <!-- Logo / Brand -->
  <div class="flex items-center gap-3 px-4 h-14 border-b border-border shrink-0">
    <div class="w-8 h-8 rounded-lg bg-primary flex items-center justify-center shrink-0">
      <span class="text-text-on-primary font-bold text-sm">A</span>
    </div>
    <div class={cn(
      'overflow-hidden transition-opacity duration-200',
      collapsed ? 'md:opacity-0 md:w-0' : 'opacity-100',
      'lg:opacity-100 lg:w-auto'
    )}>
      <h1 class="text-base font-semibold text-text-primary leading-tight">Arkive</h1>
      <p class="text-[10px] text-text-secondary tracking-wider uppercase">Disaster Recovery</p>
    </div>

    <!-- Mobile close button -->
    <button
      class="ml-auto p-1 rounded-md hover:bg-bg-surface-hover lg:hidden"
      on:click={handleOverlayClick}
      aria-label="Close sidebar"
    >
      <X class="w-4 h-4 text-text-secondary" />
    </button>
  </div>

  <!-- Navigation -->
  <nav class="flex-1 overflow-y-auto px-3 py-4 space-y-1">
    {#each navItems as item}
      <a
        href={item.href}
        on:click={handleNavClick}
        class={cn(
          'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
          isActive(item.href)
            ? 'bg-primary-bg text-primary font-medium'
            : 'text-text-secondary hover:text-text-primary hover:bg-bg-surface-hover'
        )}
        title={collapsed ? item.label : undefined}
      >
        <svelte:component this={item.icon} class="w-4 h-4 shrink-0" />
        <span class={cn(
          'transition-opacity duration-200 truncate',
          collapsed ? 'md:opacity-0 md:w-0 md:overflow-hidden' : '',
          'lg:opacity-100 lg:w-auto'
        )}>
          {item.label}
        </span>
      </a>
    {/each}

    <!-- Settings section -->
    <div class="pt-4 border-t border-border-muted mt-4">
      <button
        on:click={toggleSettings}
        class={cn(
          'flex items-center justify-between w-full px-3 py-2 text-sm rounded-md transition-colors',
          isSettingsActive
            ? 'text-primary font-medium'
            : 'text-text-secondary hover:text-text-primary hover:bg-bg-surface-hover'
        )}
        title={collapsed ? 'Settings' : undefined}
      >
        <span class="flex items-center gap-3">
          <Settings class="w-4 h-4 shrink-0" />
          <span class={cn(
            'transition-opacity duration-200',
            collapsed ? 'md:opacity-0 md:w-0 md:overflow-hidden' : '',
            'lg:opacity-100 lg:w-auto'
          )}>
            Settings
          </span>
        </span>
        <ChevronRight
          class={cn(
            'w-4 h-4 transition-transform duration-200',
            settingsExpanded && 'rotate-90',
            collapsed ? 'md:hidden' : '',
            'lg:block'
          )}
        />
      </button>

      {#if settingsExpanded && !collapsed}
        <div class="ml-7 mt-1 space-y-0.5">
          {#each settingsItems as item}
            <a
              href={item.href}
              on:click={handleNavClick}
              class={cn(
                'block px-3 py-1.5 text-sm rounded-md transition-colors',
                $page.url.pathname === item.href
                  ? 'text-primary font-medium bg-primary-bg'
                  : 'text-text-secondary hover:text-text-primary hover:bg-bg-surface-hover'
              )}
            >
              {item.label}
            </a>
          {/each}
        </div>
      {/if}

      <!-- Settings sub-items visible on desktop even when expanded -->
      {#if settingsExpanded && collapsed}
        <!-- When collapsed on tablet, settings sub-items are hidden; user clicks settings icon to go to /settings -->
      {/if}
    </div>
  </nav>

  <!-- Version footer -->
  <div class="px-4 py-3 border-t border-border-muted shrink-0">
    <div class={cn(
      'flex items-center justify-between text-[11px] text-text-muted',
      collapsed ? 'md:justify-center' : ''
    )}>
      <span class={cn(
        collapsed ? 'md:hidden' : '',
        'lg:inline'
      )}>v3.0.0</span>
      <span class={cn(
        'inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium',
        'bg-success/10 text-success',
        collapsed ? 'md:hidden' : '',
        'lg:inline-flex'
      )}>
        Community
      </span>
    </div>
  </div>
</aside>
