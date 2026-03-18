import { writable, derived } from 'svelte/store';
import type { SystemStatus, VerificationStatus } from '$lib/types';
import { getStatus } from '$lib/api/health';

export const status = writable<SystemStatus | null>(null);
export const loading = writable(true);
export const error = writable<string | null>(null);
export const verificationStatus = writable<VerificationStatus | null>(null);

export const isSetupComplete = derived(status, ($status) => $status?.setup_complete ?? false);
export const isBackupRunning = derived(status, ($status) => $status?.backup_running ?? false);

export async function refreshStatus() {
  try {
    loading.set(true);
    error.set(null);
    const data = await getStatus();
    status.set(data);
    const vs: VerificationStatus = {
      trust_score: data.trust_score ?? data.verification_status?.trust_score ?? null,
      last_verified_at: data.last_verified_at ?? data.verification_status?.last_verified_at ?? null,
      verification_passing: data.verification_status?.verification_passing ?? (data.trust_score > 0),
    };
    verificationStatus.set(vs);
  } catch (e) {
    error.set(e instanceof Error ? e.message : 'Failed to fetch status');
  } finally {
    loading.set(false);
  }
}
