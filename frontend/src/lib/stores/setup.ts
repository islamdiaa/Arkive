import { writable } from 'svelte/store';
import type { DiscoveredContainer } from '$lib/types';

export const currentStep = writable(1);
export const totalSteps = writable(6);
export const discoveredContainers = writable<DiscoveredContainer[]>([]);
export const selectedDatabases = writable<string[]>([]);
export const selectedDirectories = writable<string[]>([]);
export const configuredTargets = writable<Record<string, unknown>[]>([]);
export const schedules = writable({
  db_dump_cron: '0 6,18 * * *',
  cloud_sync_cron: '0 7 * * *',
  flash_backup_cron: '0 5 * * *',
});
export const resticPassword = writable('');
export const setupLoading = writable(false);
export const setupError = writable<string | null>(null);
