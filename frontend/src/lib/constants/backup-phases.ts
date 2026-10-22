export const BACKUP_PHASES = [
  { key: 'discovering', label: 'Discovery' },
  { key: 'dumping_databases', label: 'DB Dumps' },
  { key: 'flash_backup', label: 'Flash' },
  { key: 'uploading', label: 'Upload' },
  { key: 'retention_cleanup', label: 'Retention' },
  { key: 'refreshing_snapshots', label: 'Snapshots' },
] as const;

export type BackupPhase = typeof BACKUP_PHASES[number];
export type BackupPhaseKey = BackupPhase['key'];

function canonicalIndex(key: string): number {
  return BACKUP_PHASES.findIndex(p => p.key === key);
}

/** Phases a job of the given type actually runs (unknown type → all phases). */
export function phasesForJobType(type: string | null | undefined): BackupPhase[] {
  if (type === 'db_dump') return BACKUP_PHASES.filter(p => p.key !== 'flash_backup');
  if (type === 'flash') return BACKUP_PHASES.filter(p => p.key !== 'dumping_databases');
  return [...BACKUP_PHASES];
}

/**
 * Index of a backend phase string (e.g. 'uploading:target-name') within the
 * given phase list. A known phase that the list omits maps to the next phase
 * the list does contain; an unknown phase maps to the last phase.
 */
export function phaseIndexIn(phases: readonly BackupPhase[], phase: string | null | undefined): number {
  if (!phase) return 0;
  const normalized = phase.split(':')[0];
  const idx = phases.findIndex(p => p.key === normalized);
  if (idx >= 0) return idx;
  const ci = canonicalIndex(normalized);
  if (ci < 0) return Math.max(phases.length - 1, 0);
  const next = phases.findIndex(p => canonicalIndex(p.key) > ci);
  return next >= 0 ? next : Math.max(phases.length - 1, 0);
}

export function phaseToIndex(phase: string): number {
  return phaseIndexIn(BACKUP_PHASES, phase);
}
