/**
 * Data loaders — plain TypeScript functions that fetch data from the API.
 * These are NOT Svelte components, so Svelte 5's compiler won't wrap
 * the await expressions with $.track_reactivity_loss().
 */
import { api } from './client';

export interface DashboardData {
  status: any;
  jobs: any[];
  activity: any[];
  storage: any;
  error: string | null;
  /** Set when the jobs request failed, so the UI never claims "no jobs". */
  jobsError: string | null;
  activityError: string | null;
}

export async function loadDashboard(): Promise<DashboardData> {
  const result: DashboardData = {
    status: null,
    jobs: [],
    activity: [],
    storage: null,
    error: null,
    jobsError: null,
    activityError: null,
  };

  try {
    result.status = await api.getStatus();
  } catch (e: any) {
    result.error = 'Status: ' + (e.message || String(e));
  }

  try {
    const j = await api.listJobs();
    result.jobs = j.items || j.jobs || [];
  } catch (e: any) {
    result.jobsError = e?.message || String(e);
  }

  try {
    const a = await api.listActivity(10);
    result.activity = a.activities || [];
  } catch (e: any) {
    result.activityError = e?.message || String(e);
  }

  try {
    result.storage = await api.getStorageStats();
  } catch {}

  return result;
}

export async function loadBackups() {
  const [jobs, targets, snapshots] = await Promise.all([
    api.listJobs().catch(() => ({ items: [], jobs: [] })),
    api.listTargets().catch(() => ({ items: [], targets: [] })),
    api.listSnapshots().catch(() => ({ items: [], snapshots: [] })),
  ]);
  return {
    jobs: jobs.items || jobs.jobs || [],
    targets: targets.items || targets.targets || [],
    snapshots: snapshots.items || snapshots.snapshots || [],
  };
}

export async function loadDatabases() {
  const [databases, containers] = await Promise.all([
    api.listDatabases().catch(() => ({ items: [], databases: [] })),
    api.listContainers().catch(() => ({ items: [], containers: [] })),
  ]);
  return {
    databases: databases.items || databases.databases || [],
    containers: containers.items || containers.containers || [],
  };
}

export async function loadActivity(limit: number = 50) {
  const a = await api.listActivity(limit);
  return a.activities || [];
}

export async function loadLogs(lines?: number) {
  const l = await api.getLogs(lines);
  return l.items || l.logs || [];
}

export async function loadTargets() {
  const t = await api.listTargets();
  return t.items || t.targets || [];
}

export async function loadSnapshots(targetId?: string) {
  const s = await api.listSnapshots(targetId);
  return s.items || s.snapshots || [];
}

export async function loadSettings() {
  return api.getSettings();
}

export async function loadChannels() {
  const c = await api.listChannels();
  return c.items || c.channels || [];
}

export async function loadDirectories() {
  const d = await api.listDirectories();
  return d.directories || [];
}

export async function loadRestorePlan() {
  const [targets, snapshots, databases] = await Promise.all([
    api.listTargets().catch(() => ({ items: [], targets: [] })),
    api.listSnapshots().catch(() => ({ items: [], snapshots: [] })),
    api.listDatabases().catch(() => ({ items: [], databases: [] })),
  ]);
  return {
    targets: targets.items || targets.targets || [],
    snapshots: snapshots.items || snapshots.snapshots || [],
    databases: databases.items || databases.databases || [],
  };
}
