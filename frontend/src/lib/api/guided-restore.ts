/**
 * Guided database restore: load one database back into its container from a
 * snapshot (backend: /api/restore/database, services/guided_restore.py).
 */
import { api } from './client';

export type RestoreMethod = 'load' | 'manual' | 'unsupported';
export type GuidedRestoreStatus =
  | 'running'
  | 'completed'
  | 'completed_with_warnings'
  | 'failed'
  | 'action_required';
export type GuidedStepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

export interface RestoreDependent {
  name: string;
  status: string | null;
  reason: string;
}

export interface RestoreOption {
  container_name: string;
  db_type: string;
  db_name: string;
  host_path: string | null;
  container_status: string | null;
  method: RestoreMethod;
  reason: string | null;
  dependents: RestoreDependent[];
}

export interface RestoreSelection {
  container_name: string;
  db_type: string;
  db_name: string;
  host_path?: string | null;
  snapshot_id: string;
  target_id: string;
}

export interface RestorePreview {
  method: RestoreMethod;
  database: {
    container_name: string;
    db_type: string;
    db_name: string;
    host_path: string | null;
    container_detail: string | null;
  };
  snapshot: { id: string; full_id: string; time: string | null; target_id: string; target_name: string };
  dump: { file: string; size_bytes: number; created_at: string | null; source: 'manifest' | 'file_name' };
  dependents: RestoreDependent[];
  confirm_phrase: string | null;
  steps: { key: string; label: string }[];
  blockers: string[];
  warnings: string[];
  safety_dump_retention_days: number;
}

export interface GuidedRestoreStep {
  key: string;
  label: string;
  status: GuidedStepStatus;
  detail: string | null;
}

export interface ManualInstruction {
  text: string;
  command: string | null;
}

export interface GuidedRestore {
  id: string;
  kind: 'restore' | 'undo';
  parent_id: string | null;
  status: GuidedRestoreStatus;
  phase: string | null;
  method: RestoreMethod;
  container_name: string;
  db_type: string;
  db_name: string;
  host_path: string | null;
  target_id: string | null;
  target_name: string | null;
  snapshot_id: string | null;
  dump_file: string | null;
  dump_size_bytes: number;
  safety_dump: { file: string; path: string; size_bytes: number; available: boolean } | null;
  database_changed: boolean | null;
  stop_containers: string[];
  stopped_containers: string[];
  steps: GuidedRestoreStep[];
  instructions: ManualInstruction[];
  warnings: string[];
  progress: { bytes_done: number; bytes_total: number } | null;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  can_undo: boolean;
}

export async function listRestoreOptions(): Promise<RestoreOption[]> {
  const result: any = await api.get('/restore/database/options');
  return Array.isArray(result?.databases) ? result.databases : [];
}

export async function previewRestore(selection: RestoreSelection): Promise<RestorePreview> {
  return api.post('/restore/database/preview', selection);
}

/** Start a restore. `confirm` must equal the preview's confirm_phrase for in-place loads. */
export async function startRestore(
  selection: RestoreSelection,
  confirm: string,
  stopContainers: string[],
): Promise<{ id: string; status: GuidedRestoreStatus }> {
  return api.post('/restore/database', { ...selection, confirm, stop_containers: stopContainers });
}

export async function getRestore(id: string): Promise<GuidedRestore> {
  return api.get(`/restore/database/${encodeURIComponent(id)}`);
}

export async function listRestores(limit = 10): Promise<{ items: GuidedRestore[]; total: number }> {
  const result: any = await api.get(`/restore/database?limit=${limit}`);
  return {
    items: Array.isArray(result?.items) ? result.items : [],
    total: typeof result?.total === 'number' ? result.total : 0,
  };
}

/** Load the safety dump of `id` back. Omitting stopContainers stops the same apps the restore did. */
export async function undoRestore(
  id: string,
  confirm: string,
  stopContainers?: string[],
): Promise<{ id: string; status: GuidedRestoreStatus }> {
  const body: Record<string, unknown> = { confirm };
  if (stopContainers) body.stop_containers = stopContainers;
  return api.post(`/restore/database/${encodeURIComponent(id)}/undo`, body);
}

// ── Pure helpers (unit-tested) ──

/** The typed confirmation must match exactly: no trimming, no case folding. */
export function confirmationMatches(typed: string, phrase: string | null | undefined): boolean {
  return !!phrase && typed === phrase;
}

export function isFinished(status: GuidedRestoreStatus | string | undefined): boolean {
  return !!status && status !== 'running';
}

/** Badge classes (app.css) for an operation status. */
export function statusBadgeClass(status: GuidedRestoreStatus | string): string {
  switch (status) {
    case 'completed':
      return 'badge-success';
    case 'completed_with_warnings':
      return 'badge-warning';
    case 'failed':
      return 'badge-danger';
    case 'action_required':
      return 'badge-info';
    case 'running':
      return 'badge-info animate-status-pulse';
    default:
      return 'badge bg-neutral-bg text-neutral';
  }
}

export function statusLabel(status: GuidedRestoreStatus | string): string {
  switch (status) {
    case 'completed':
      return 'Completed';
    case 'completed_with_warnings':
      return 'Completed with warnings';
    case 'failed':
      return 'Failed';
    case 'action_required':
      return 'Action required';
    case 'running':
      return 'Running';
    default:
      return status || 'Unknown';
  }
}

export function methodLabel(method: RestoreMethod): string {
  if (method === 'load') return 'Restores in place';
  if (method === 'manual') return 'Prepared, swapped in by hand';
  return 'Not supported';
}

/** Percent of the dump streamed into the database, or null outside the load step. */
export function loadPercent(op: Pick<GuidedRestore, 'progress'>): number | null {
  const p = op.progress;
  if (!p || !p.bytes_total) return null;
  return Math.max(0, Math.min(100, Math.round((p.bytes_done / p.bytes_total) * 100)));
}

/** Running apps are suggested for stopping by default; stopped ones need no action. */
export function defaultStopSelection(dependents: RestoreDependent[]): string[] {
  return dependents.filter((d) => d.status === 'running').map((d) => d.name);
}

/** Key that identifies one database in the options list. */
export function optionKey(o: Pick<RestoreOption, 'container_name' | 'db_type' | 'db_name' | 'host_path'>): string {
  return [o.container_name, o.db_type, o.db_name, o.host_path ?? ''].join('\u0000');
}
