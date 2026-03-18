/**
 * Mock API client for demo/preview mode.
 * Returns realistic demo data so the UI can render without the FastAPI backend.
 */

const DEMO_STATUS = {
  version: '0.1.0',
  uptime_seconds: 86400,
  setup_completed: true,
  platform: 'unraid',
  hostname: 'tower',
  status: 'ok',
  health: 'healthy',
  containers_discovered: 14,
  databases_found: 6,
  targets_configured: 2,
  total_snapshots: 47,
  storage_used_bytes: 12884901888,
  targets: { total: 2, healthy: 2 },
  databases: { total: 6, healthy: 6 },
  storage: { total_bytes: 12884901888 },
  last_backup: {
    status: 'success',
    started_at: new Date(Date.now() - 720000).toISOString(),
    completed_at: new Date(Date.now() - 378000).toISOString(),
    duration_seconds: 342,
  },
  last_backup_status: 'success',
  next_backup: new Date(Date.now() + 7200000).toISOString(),
  checks: {
    database: { ok: true, message: 'connected' },
    scheduler: { ok: true, message: 'running' },
    disk: { ok: true, free_bytes: 53687091200, total_bytes: 107374182400, used_percent: 50.0, message: '50.0% used, 51200 MB free' },
    binaries: { ok: true, message: 'restic and rclone available' },
  },
  coverage: {
    readiness: 'migration_ready',
    migration_ready: true,
    appdata_protected: true,
    flash_protected: true,
    watched_directories: 2,
    protected_directories: ['/mnt/user/appdata', '/mnt/user/domains'],
    recommended_directories: [],
    warnings: [],
  },
};

const DEMO_JOBS = {
  jobs: [
    {
      id: 'job-daily-full',
      name: 'Daily Full Backup',
      type: 'full',
      schedule: '0 3 * * *',
      enabled: true,
      targets: ['target-b2-main'],
      directories: ['/mnt/user/appdata', '/mnt/user/domains'],
      exclude_patterns: [],
      include_databases: true,
      include_flash: true,
      last_run: { started_at: new Date(Date.now() - 720000).toISOString(), status: 'success' },
      next_run: new Date(Date.now() + 7200000).toISOString(),
      created_at: '2026-01-15T10:00:00Z',
      updated_at: '2026-02-20T08:00:00Z',
    },
    {
      id: 'job-hourly-db',
      name: 'Hourly DB Snapshots',
      type: 'db_dump',
      schedule: '0 * * * *',
      enabled: true,
      targets: ['target-b2-main'],
      directories: [],
      exclude_patterns: [],
      include_databases: true,
      include_flash: false,
      last_run: { started_at: new Date(Date.now() - 3600000).toISOString(), status: 'success' },
      next_run: new Date(Date.now() + 600000).toISOString(),
      created_at: '2026-01-15T10:00:00Z',
      updated_at: '2026-02-20T08:00:00Z',
    },
  ],
};

const DEMO_TARGETS = {
  targets: [
    {
      id: 'target-b2-main',
      name: 'Backblaze B2 — Primary',
      provider: 'b2',
      bucket: 'arkive-tower-backups',
      healthy: true,
      last_check: new Date(Date.now() - 300000).toISOString(),
      used_bytes: 12884901888,
      total_bytes: 107374182400,
      created_at: '2026-01-15T10:00:00Z',
    },
    {
      id: 'target-sftp-nas',
      name: 'SFTP — TrueNAS',
      provider: 'sftp',
      host: '192.168.1.50',
      healthy: true,
      last_check: new Date(Date.now() - 600000).toISOString(),
      used_bytes: 8589934592,
      total_bytes: 53687091200,
      created_at: '2026-01-20T14:00:00Z',
    },
  ],
};

const DEMO_DATABASES = {
  databases: [
    { container: 'immich-postgres', engine: 'postgres', name: 'immich', size_bytes: 2147483648, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
    { container: 'vaultwarden-db', engine: 'postgres', name: 'vaultwarden', size_bytes: 52428800, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
    { container: 'paperless-db', engine: 'postgres', name: 'paperless', size_bytes: 1073741824, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
    { container: 'nextcloud-mariadb', engine: 'mariadb', name: 'nextcloud', size_bytes: 3221225472, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
    { container: 'authelia-redis', engine: 'redis', name: 'authelia', size_bytes: 10485760, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
    { container: 'home-assistant-influx', engine: 'influxdb', name: 'homeassistant', size_bytes: 5368709120, last_dump: new Date(Date.now() - 720000).toISOString(), status: 'healthy' },
  ],
};

const DEMO_ACTIVITY = {
  activities: [
    { id: '1', type: 'backup_complete', action: 'backup_completed', message: 'Daily Full Backup completed successfully', timestamp: new Date(Date.now() - 720000).toISOString(), severity: 'success', details: { duration_seconds: 342, databases: 6, size_bytes: 12884901888 } },
    { id: '2', type: 'snapshot_created', action: 'snapshot_created', message: 'Snapshot created on Backblaze B2', timestamp: new Date(Date.now() - 740000).toISOString(), severity: 'info', details: { target: 'Backblaze B2 — Primary', snapshot_id: 'abc123' } },
    { id: '3', type: 'db_dump', action: 'db_dumped', message: 'Dumped 6 databases (11.2 GB total)', timestamp: new Date(Date.now() - 760000).toISOString(), severity: 'info', details: { count: 6 } },
    { id: '4', type: 'discovery', action: 'discovery_completed', message: 'Discovered 14 containers, 6 databases', timestamp: new Date(Date.now() - 780000).toISOString(), severity: 'info', details: { containers: 14, databases: 6 } },
    { id: '5', type: 'retention', action: 'retention_pruned', message: 'Pruned 3 old snapshots per retention policy', timestamp: new Date(Date.now() - 86400000).toISOString(), severity: 'info', details: { pruned: 3 } },
    { id: '6', type: 'backup_complete', action: 'backup_completed', message: 'Hourly DB Snapshots completed', timestamp: new Date(Date.now() - 3600000).toISOString(), severity: 'success', details: { duration_seconds: 45 } },
    { id: '7', type: 'target_check', action: 'target_checked', message: 'All storage targets healthy', timestamp: new Date(Date.now() - 7200000).toISOString(), severity: 'info', details: {} },
    { id: '8', type: 'notification', action: 'notification_sent', message: 'Backup summary sent to Discord', timestamp: new Date(Date.now() - 720000).toISOString(), severity: 'info', details: { channel: 'Discord' } },
  ],
  total: 8,
};

const DEMO_STORAGE = {
  targets: [
    { id: 'target-b2-main', name: 'Backblaze B2 — Primary', used_bytes: 12884901888, total_bytes: 107374182400, snapshot_count: 35 },
    { id: 'target-sftp-nas', name: 'SFTP — TrueNAS', used_bytes: 8589934592, total_bytes: 53687091200, snapshot_count: 12 },
  ],
  total_used_bytes: 21474836480,
  total_snapshots: 47,
};

const DEMO_SNAPSHOTS = {
  snapshots: [
    { id: 'snap-001', target_id: 'target-b2-main', target_name: 'Backblaze B2', created_at: new Date(Date.now() - 720000).toISOString(), size_bytes: 2147483648, databases: ['immich', 'vaultwarden', 'paperless', 'nextcloud', 'authelia', 'homeassistant'], directories: ['/mnt/user/appdata', '/boot'], type: 'full' },
    { id: 'snap-002', target_id: 'target-b2-main', target_name: 'Backblaze B2', created_at: new Date(Date.now() - 86400000).toISOString(), size_bytes: 1073741824, databases: ['immich', 'vaultwarden', 'paperless', 'nextcloud', 'authelia', 'homeassistant'], directories: ['/mnt/user/appdata', '/boot'], type: 'full' },
    { id: 'snap-003', target_id: 'target-sftp-nas', target_name: 'SFTP — TrueNAS', created_at: new Date(Date.now() - 172800000).toISOString(), size_bytes: 2147483648, databases: ['immich', 'vaultwarden', 'paperless', 'nextcloud', 'authelia', 'homeassistant'], directories: ['/mnt/user/appdata'], type: 'full' },
  ],
};

const DEMO_CONTAINERS = {
  containers: [
    { name: 'immich-server', image: 'ghcr.io/immich-app/immich-server:release', status: 'running', has_database: false },
    { name: 'immich-postgres', image: 'tensorchord/pgvecto-rs:pg14-v0.2.0', status: 'running', has_database: true, db_engine: 'postgres' },
    { name: 'vaultwarden', image: 'vaultwarden/server:latest', status: 'running', has_database: false },
    { name: 'vaultwarden-db', image: 'postgres:16-alpine', status: 'running', has_database: true, db_engine: 'postgres' },
    { name: 'paperless-ngx', image: 'ghcr.io/paperless-ngx/paperless-ngx:latest', status: 'running', has_database: false },
    { name: 'paperless-db', image: 'postgres:16-alpine', status: 'running', has_database: true, db_engine: 'postgres' },
    { name: 'nextcloud', image: 'nextcloud:latest', status: 'running', has_database: false },
    { name: 'nextcloud-mariadb', image: 'mariadb:11', status: 'running', has_database: true, db_engine: 'mariadb' },
    { name: 'authelia', image: 'authelia/authelia:latest', status: 'running', has_database: false },
    { name: 'authelia-redis', image: 'redis:7-alpine', status: 'running', has_database: true, db_engine: 'redis' },
    { name: 'home-assistant', image: 'ghcr.io/home-assistant/home-assistant:stable', status: 'running', has_database: false },
    { name: 'home-assistant-influx', image: 'influxdb:2.7', status: 'running', has_database: true, db_engine: 'influxdb' },
    { name: 'plex', image: 'plexinc/pms-docker:latest', status: 'running', has_database: false },
    { name: 'sonarr', image: 'lscr.io/linuxserver/sonarr:latest', status: 'running', has_database: false },
  ],
};

const DEMO_SETTINGS = {
  encryption_password_set: true,
  schedule: '0 3 * * *',
  retention: { keep_daily: 7, keep_weekly: 4, keep_monthly: 6 },
  notifications_enabled: true,
  flash_backup_enabled: true,
  auto_discovery: true,
  discovery_interval_minutes: 60,
};

const DEMO_CHANNELS = {
  channels: [
    { id: 'ch-discord', type: 'discord', name: 'Homelab Alerts', enabled: true, webhook_url: 'https://discord.com/api/webhooks/...', last_sent: new Date(Date.now() - 720000).toISOString() },
  ],
};

const DEMO_DIRECTORIES = {
  directories: [
    { id: 'dir-appdata', path: '/mnt/user/appdata', enabled: true, size_bytes: 53687091200, last_scan: new Date(Date.now() - 3600000).toISOString() },
    { id: 'dir-domains', path: '/mnt/user/domains', enabled: true, size_bytes: 10737418240, last_scan: new Date(Date.now() - 3600000).toISOString() },
  ],
};

const DEMO_DIR_SUGGESTIONS = [
  { path: '/mnt/user/system', priority: 'critical', size_bytes: 1073741824, file_count: 342, reason: 'System configuration' },
  { path: '/mnt/user/docker', priority: 'recommended', size_bytes: 5368709120, file_count: 1204, reason: 'Docker persistent data' },
  { path: '/mnt/user/domains', priority: 'recommended', size_bytes: 10737418240, file_count: 87, reason: 'Virtual machine configs' },
  { path: '/mnt/user/photos', priority: 'optional', size_bytes: 107374182400, file_count: 24000, reason: 'Personal photos (irreplaceable)' },
];

const DEMO_LOGS = {
  logs: [
    `[2026-02-26T03:00:00Z] INFO  Starting daily backup run...`,
    `[2026-02-26T03:00:01Z] INFO  Discovery: scanning Docker socket...`,
    `[2026-02-26T03:00:02Z] INFO  Found 14 containers, 6 databases`,
    `[2026-02-26T03:00:03Z] INFO  Dumping postgres: immich (2.0 GB)`,
    `[2026-02-26T03:00:45Z] INFO  Dumping postgres: vaultwarden (50 MB)`,
    `[2026-02-26T03:00:50Z] INFO  Dumping postgres: paperless (1.0 GB)`,
    `[2026-02-26T03:01:20Z] INFO  Dumping mariadb: nextcloud (3.0 GB)`,
    `[2026-02-26T03:02:00Z] INFO  Dumping redis: authelia (10 MB)`,
    `[2026-02-26T03:02:05Z] INFO  Dumping influxdb: homeassistant (5.0 GB)`,
    `[2026-02-26T03:03:30Z] INFO  Creating restic snapshot...`,
    `[2026-02-26T03:04:00Z] INFO  Snapshot created: abc123def`,
    `[2026-02-26T03:04:01Z] INFO  Syncing to Backblaze B2...`,
    `[2026-02-26T03:05:42Z] INFO  Backup complete. Duration: 5m42s. Size: 12.0 GB`,
    `[2026-02-26T03:05:43Z] INFO  Notification sent to Discord`,
    `[2026-02-26T03:05:44Z] INFO  Retention: pruned 2 old snapshots`,
  ].join('\n'),
};

// Simulate a short delay
const delay = (ms: number = 200) => new Promise(r => setTimeout(r, ms));

function _stripQuery(path: string): string {
  return path.split('?')[0];
}

async function _mockGet(path: string): Promise<any> {
  const clean = _stripQuery(path);

  if (clean === '/status') return DEMO_STATUS;
  if (clean === '/jobs') return DEMO_JOBS;
  if (clean.startsWith('/jobs/')) {
    const parts = clean.split('/').filter(Boolean);
    if (parts[1] === 'runs' && parts[2]) {
      return {
        id: parts[2],
        status: 'success',
        started_at: new Date(Date.now() - 720000).toISOString(),
        finished_at: new Date(Date.now() - 378000).toISOString()
      };
    }
    if (parts[1]) return DEMO_JOBS.jobs.find(j => j.id === parts[1]) || DEMO_JOBS.jobs[0];
  }
  if (clean === '/targets') return DEMO_TARGETS;
  if (clean.startsWith('/targets/')) {
    const id = clean.split('/')[2];
    return DEMO_TARGETS.targets.find(t => t.id === id) || DEMO_TARGETS.targets[0];
  }
  if (clean === '/databases') return DEMO_DATABASES;
  if (clean === '/notifications') return DEMO_CHANNELS;
  if (clean === '/settings') return DEMO_SETTINGS;
  if (clean === '/activity') return DEMO_ACTIVITY;
  if (clean === '/storage') return DEMO_STORAGE;
  if (clean.startsWith('/snapshots')) return DEMO_SNAPSHOTS;
  if (clean === '/discover/containers') return DEMO_CONTAINERS;
  if (clean === '/directories') return DEMO_DIRECTORIES;
  if (clean === '/logs') return DEMO_LOGS;

  return {};
}

async function _mockPost(path: string, data?: any): Promise<any> {
  const clean = _stripQuery(path);

  if (clean === '/auth/login') return { setup_required: false, authenticated: true, setup_completed_at: new Date().toISOString() };
  if (clean === '/auth/logout') return { authenticated: false, message: 'Logged out' };
  if (clean === '/auth/setup') return { success: true, api_key: 'demo-api-key-12345' };
  if (clean === '/auth/rotate-key') return { api_key: 'demo-new-key-67890' };
  if (clean === '/jobs') return { ...data, id: `job-new-${Date.now()}` };
  if (clean.endsWith('/run')) return { run_id: `run-demo-${Date.now()}` };
  if (clean === '/targets') return { ...data, id: `target-new-${Date.now()}` };
  if (clean.endsWith('/test')) return { success: true };
  if (clean === '/restore') return { success: true, restore_id: `restore-${Date.now()}` };
  if (clean === '/discover/scan') return DEMO_CONTAINERS;
  if (clean === '/directories/scan') return { ...DEMO_DIRECTORIES, suggestions: DEMO_DIR_SUGGESTIONS, platform: 'unraid' };
  if (clean === '/directories') return { ...data, id: `dir-new-${Date.now()}` };

  return { success: true };
}

async function _mockPut(_path: string, data?: any): Promise<any> {
  return data ?? { success: true };
}

async function _mockDelete(_path: string): Promise<any> {
  return { success: true };
}

export const mockApi = {
  get: async (path: string) => { await delay(); return _mockGet(path); },
  post: async (path: string, data?: any) => { await delay(); return _mockPost(path, data); },
  put: async (path: string, data?: any) => { await delay(); return _mockPut(path, data); },
  delete: async (path: string) => { await delay(); return _mockDelete(path); },

  getStatus: async () => { await delay(); return DEMO_STATUS; },
  getSession: async () => { await delay(); return { setup_required: false, authenticated: true, setup_completed_at: new Date().toISOString() }; },
  login: async (_apiKey: string) => { await delay(); return { setup_required: false, authenticated: true, setup_completed_at: new Date().toISOString() }; },
  logout: async () => { await delay(); return { authenticated: false, message: 'Logged out' }; },
  completeSetup: async (_data: any) => { await delay(500); return { success: true, api_key: 'demo-api-key-12345', setup_completed_at: new Date().toISOString() }; },
  rotateKey: async () => { await delay(); return { api_key: 'demo-new-key-67890' }; },

  listJobs: async () => { await delay(); return DEMO_JOBS; },
  getJob: async (id: string) => { await delay(); return DEMO_JOBS.jobs.find(j => j.id === id) || DEMO_JOBS.jobs[0]; },
  createJob: async (data: any) => { await delay(500); return { ...data, id: 'job-new-' + Date.now() }; },
  updateJob: async (_id: string, data: any) => { await delay(); return data; },
  deleteJob: async (_id: string) => { await delay(); return { success: true }; },
  triggerJob: async (_id: string) => { await delay(300); return { run_id: 'run-demo-' + Date.now() }; },
  listRuns: async (_jobId: string) => { await delay(); return { runs: [{ id: 'run-1', job_id: _jobId, status: 'success', started_at: new Date(Date.now() - 720000).toISOString(), finished_at: new Date(Date.now() - 378000).toISOString(), duration_seconds: 342 }] }; },
  getRun: async (_jobId: string, _runId: string) => { await delay(); return { id: _runId, job_id: _jobId, status: 'success', started_at: new Date(Date.now() - 720000).toISOString(), finished_at: new Date(Date.now() - 378000).toISOString(), duration_seconds: 342, phases: ['discovery', 'db_dump', 'snapshot', 'sync', 'notify'] }; },
  cancelRun: async () => { await delay(); return { success: true }; },

  listTargets: async () => { await delay(); return DEMO_TARGETS; },
  getTarget: async (id: string) => { await delay(); return DEMO_TARGETS.targets.find(t => t.id === id) || DEMO_TARGETS.targets[0]; },
  createTarget: async (data: any) => { await delay(500); return { ...data, id: 'target-new-' + Date.now() }; },
  updateTarget: async (_id: string, data: any) => { await delay(); return data; },
  deleteTarget: async (_id: string) => { await delay(); return { success: true }; },
  testTarget: async (_id: string) => { await delay(1000); return { healthy: true, latency_ms: 45 }; },

  listSnapshots: async () => { await delay(); return DEMO_SNAPSHOTS; },
  refreshSnapshots: async () => { await delay(1000); return DEMO_SNAPSHOTS; },
  browseSnapshot: async (_id: string, _path: string = '/') => { await delay(); return { path: _path, entries: [{ name: 'appdata', type: 'dir', size: 0 }, { name: 'boot', type: 'dir', size: 0 }, { name: 'db-dumps', type: 'dir', size: 0 }] }; },

  restore: async (_data: any) => { await delay(500); return { success: true, restore_id: 'restore-' + Date.now() }; },
  downloadRestorePlan: () => '#',

  getSettings: async () => { await delay(); return DEMO_SETTINGS; },
  updateSettings: async (data: any) => { await delay(); return { ...DEMO_SETTINGS, ...data }; },

  listChannels: async () => { await delay(); return DEMO_CHANNELS; },
  createChannel: async (data: any) => { await delay(500); return { ...data, id: 'ch-new-' + Date.now() }; },
  updateChannel: async (_id: string, data: any) => { await delay(); return data; },
  deleteChannel: async (_id: string) => { await delay(); return { success: true }; },
  testChannel: async (_id: string) => { await delay(1000); return { success: true }; },

  listActivity: async (_limit?: number, _offset?: number) => { await delay(); return DEMO_ACTIVITY; },

  getStorageStats: async () => { await delay(); return DEMO_STORAGE; },

  runScan: async () => { await delay(1500); return DEMO_CONTAINERS; },
  listContainers: async () => { await delay(); return DEMO_CONTAINERS; },

  listDatabases: async () => { await delay(); return DEMO_DATABASES; },
  dumpDatabase: async () => { await delay(1000); return { success: true }; },

  listDirectories: async () => { await delay(); return DEMO_DIRECTORIES; },
  addDirectory: async (data: any) => { await delay(500); return { ...data, id: 'dir-new-' + Date.now() }; },
  updateDirectory: async (_id: string, data: any) => { await delay(); return data; },
  deleteDirectory: async (_id: string) => { await delay(); return { success: true }; },
  scanDirectories: async () => { await delay(1000); return { ...DEMO_DIRECTORIES, suggestions: DEMO_DIR_SUGGESTIONS, platform: 'unraid' }; },

  getLogs: async () => { await delay(); return DEMO_LOGS; },
  clearLogs: async () => { await delay(); return { success: true }; },

  createEventSource: () => {
    // Return a fake EventSource-like object
    return { close: () => {}, addEventListener: () => {}, removeEventListener: () => {} } as any;
  },
};
