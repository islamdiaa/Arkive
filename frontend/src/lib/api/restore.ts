import { api, ApiError } from './client';
import type { Snapshot, FileNode, PaginatedResponse } from '$lib/types';

/** Directories the backend accepts as `restore_to` (see RestoreRequest validator). */
export const RESTORE_ROOT_HINT = '/tmp/arkive-restore, /var/tmp/arkive-restore or <config dir>/restores';
export const DEFAULT_RESTORE_TO = '/tmp/arkive-restore';

/** Request body for POST /api/restore (backend RestoreRequest). */
export interface RestoreRequestBody {
  snapshot_id: string;
  target: string;
  paths: string[];
  restore_to?: string;
  dry_run?: boolean;
}

export function buildRestoreRequest(
  snapshot: Pick<Snapshot, 'id' | 'target_id'> & Partial<Pick<Snapshot, 'full_id' | 'short_id' | 'paths'>>,
  opts: { restoreTo?: string; dryRun?: boolean; paths?: string[] } = {},
): RestoreRequestBody {
  const body: RestoreRequestBody = {
    snapshot_id: snapshot.full_id || snapshot.id || snapshot.short_id || '',
    target: snapshot.target_id,
    paths: opts.paths ?? snapshot.paths ?? [],
    dry_run: !!opts.dryRun,
  };
  const restoreTo = opts.restoreTo?.trim();
  if (restoreTo) body.restore_to = restoreTo;
  return body;
}

export interface RestoreOutcome {
  ok: boolean;
  message: string;
}

/**
 * Interpret a /restore response. Dry runs that fail come back as HTTP 200 with
 * `status: 'failed'`; real restores report `status: success|failed|...`.
 */
export function interpretRestoreResult(result: any, dryRun: boolean): RestoreOutcome {
  const status = result?.status;
  if (dryRun) {
    if (status === 'failed' || status === 'error') {
      return { ok: false, message: result?.message || 'Dry run failed' };
    }
    const count = Array.isArray(result?.entries) ? result.entries.length : null;
    const message = count !== null
      ? `Dry run OK: ${count} ${count === 1 ? 'entry' : 'entries'} found in the snapshot. Nothing was written.`
      : result?.message || 'Dry run completed. Nothing was written.';
    return { ok: true, message };
  }
  if (status === 'success') {
    return { ok: true, message: 'Restore completed successfully.' };
  }
  return { ok: false, message: result?.message || `Restore ${status || 'failed'}` };
}

export async function listSnapshots(targetId?: string): Promise<PaginatedResponse<Snapshot>> {
  const query = targetId ? `?target_id=${encodeURIComponent(targetId)}` : '';
  return api.get(`/snapshots${query}`);
}

export async function browseSnapshot(snapshotId: string, path: string = '/'): Promise<FileNode[]> {
  return api.get(`/snapshots/${snapshotId}/browse?path=${encodeURIComponent(path)}`);
}

export async function restoreFiles(data: RestoreRequestBody): Promise<{ status: string; restore_id: string; message?: string }> {
  return api.post('/restore', data);
}

export async function getRestorePlan(): Promise<{ markdown: string }> {
  return api.get('/restore/plan');
}

export interface RestorePlanDownload {
  blob: Blob;
  filename: string;
  /** False when the server fell back to an HTML plan (PDF engine unavailable). */
  isPdf: boolean;
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : null;
}

export async function downloadRestorePlan(): Promise<RestorePlanDownload> {
  const response = await fetch('/api/restore/plan/pdf', {
    credentials: 'include',
  });
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body?.message) message = body.message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, message);
  }
  const contentType = (response.headers.get('Content-Type') || '').toLowerCase();
  const isPdf = contentType.includes('application/pdf');
  const extension = isPdf ? 'pdf' : contentType.includes('text/html') ? 'html' : 'txt';
  const filename =
    filenameFromDisposition(response.headers.get('Content-Disposition')) || `arkive-restore-plan.${extension}`;
  return { blob: await response.blob(), filename, isPdf };
}

export async function testRestore(): Promise<{ success: boolean; message: string; file_tested: string }> {
  return api.post('/restore/test');
}
