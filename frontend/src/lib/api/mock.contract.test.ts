/**
 * Mock Contract Validation Test
 *
 * Ensures that demo mock data (mock.ts) contains all the fields
 * the real API returns, preventing contract drift between
 * demo mode and production API responses.
 */
import { describe, it, expect } from 'vitest';
import { mockApi } from './mock';

/**
 * Fields the frontend dashboard reads from the /status endpoint.
 * Derived from +page.svelte helper functions and template bindings.
 */
const REQUIRED_STATUS_FIELDS = [
  'version',
  'uptime_seconds',
  'setup_completed',
  'platform',
  'hostname',
  'status',
  'health',
  'containers_discovered',
  'databases_found',
  'targets_configured',
  'total_snapshots',
  'storage_used_bytes',
  'targets',
  'databases',
  'storage',
  'last_backup',
  'last_backup_status',
  'next_backup',
  'checks',
  'coverage',
  'backup_freshness',
  'recovery_kit_confirmed',
  'last_drill',
];

/**
 * Fields the real jobs API returns per job (from _row_to_job + _enrich_job).
 */
const REQUIRED_JOB_FIELDS = [
  'id',
  'name',
  'schedule',
  'enabled',
  'targets',        // real API uses 'targets', not 'target_ids'
  'directories',    // real API uses 'directories', not 'include_directories'
  'include_databases',
  'include_flash',
  'last_run',       // real API returns object { started_at, status }, not string
  'next_run',
  'created_at',
];

/**
 * Fields the real activity API returns per entry.
 */
const REQUIRED_ACTIVITY_FIELDS = [
  'id',
  'type',
  'action',
  'message',
  'timestamp',
  'severity',
];

describe('Mock API contract validation', () => {
  it('status response contains all required dashboard fields', async () => {
    const status = await mockApi.getStatus();
    for (const field of REQUIRED_STATUS_FIELDS) {
      expect(status).toHaveProperty(field);
    }
  });

  it('status.last_backup is an object with status and started_at', async () => {
    const status = await mockApi.getStatus();
    expect(status.last_backup).toBeDefined();
    expect(typeof status.last_backup).toBe('object');
    expect(status.last_backup).toHaveProperty('status');
    expect(status.last_backup).toHaveProperty('started_at');
  });

  it('status.targets is a summary object with total and healthy', async () => {
    const status = await mockApi.getStatus();
    expect(status.targets).toHaveProperty('total');
    expect(status.targets).toHaveProperty('healthy');
  });

  it('status.databases is a summary object with total and healthy', async () => {
    const status = await mockApi.getStatus();
    expect(status.databases).toHaveProperty('total');
    expect(status.databases).toHaveProperty('healthy');
  });

  it('status.checks contains health check keys', async () => {
    const status = await mockApi.getStatus();
    expect(status.checks).toHaveProperty('database');
    expect(status.checks).toHaveProperty('scheduler');
    expect(status.checks).toHaveProperty('disk');
    expect(status.checks).toHaveProperty('binaries');
  });

  it('jobs response contains all required fields per job', async () => {
    const result = await mockApi.listJobs();
    const jobs = result.jobs;
    expect(jobs.length).toBeGreaterThan(0);
    for (const job of jobs) {
      for (const field of REQUIRED_JOB_FIELDS) {
        expect(job).toHaveProperty(field);
      }
    }
  });

  it('job.targets is an array (not target_ids)', async () => {
    const result = await mockApi.listJobs();
    for (const job of result.jobs) {
      expect(Array.isArray(job.targets)).toBe(true);
      expect(job).not.toHaveProperty('target_ids');
    }
  });

  it('job.directories is an array (not include_directories boolean)', async () => {
    const result = await mockApi.listJobs();
    for (const job of result.jobs) {
      expect(Array.isArray(job.directories)).toBe(true);
    }
  });

  it('job.last_run is an object with started_at and status', async () => {
    const result = await mockApi.listJobs();
    for (const job of result.jobs) {
      expect(typeof job.last_run).toBe('object');
      expect(job.last_run).toHaveProperty('started_at');
      expect(job.last_run).toHaveProperty('status');
    }
  });

  it('activity response contains all required fields per entry', async () => {
    const result = await mockApi.listActivity();
    const activities = result.activities;
    expect(activities.length).toBeGreaterThan(0);
    for (const entry of activities) {
      for (const field of REQUIRED_ACTIVITY_FIELDS) {
        expect(entry).toHaveProperty(field);
      }
    }
  });

  it('status.backup_freshness has stale, checked_at and stale_jobs', async () => {
    const status = await mockApi.getStatus();
    expect(typeof status.backup_freshness.stale).toBe('boolean');
    expect(status.backup_freshness).toHaveProperty('checked_at');
    expect(Array.isArray(status.backup_freshness.stale_jobs)).toBe(true);
    expect(typeof status.recovery_kit_confirmed).toBe('boolean');
  });

  it('status.last_drill matches the drill summary shape', async () => {
    const status = await mockApi.getStatus();
    for (const field of ['id', 'status', 'started_at', 'completed_at', 'databases_tested', 'databases_passed', 'databases_failed']) {
      expect(status.last_drill).toHaveProperty(field);
    }
  });

  it('drills list and detail match the contract', async () => {
    const list = await mockApi.get('/drills?limit=5&offset=0');
    expect(Array.isArray(list.items)).toBe(true);
    expect(typeof list.total).toBe('number');
    const drill = list.items[0];
    for (const field of [
      'id', 'trigger', 'status', 'target_id', 'target_name', 'snapshot_id', 'started_at', 'completed_at',
      'duration_seconds', 'databases_tested', 'databases_passed', 'databases_failed', 'error_message',
    ]) {
      expect(drill).toHaveProperty(field);
    }
    const detail = await mockApi.get(`/drills/${drill.id}`);
    expect(detail.id).toBe(drill.id);
    expect(detail.results.length).toBeGreaterThan(0);
    for (const field of ['container_name', 'db_type', 'db_name', 'dump_file', 'dump_size_bytes', 'status', 'detail', 'duration_seconds']) {
      expect(detail.results[0]).toHaveProperty(field);
    }
    const run = await mockApi.post('/drills/run');
    expect(run).toHaveProperty('drill_id');
    expect(run.status).toBe('running');
  });

  it('notification events endpoint returns events and defaults', async () => {
    const result = await mockApi.get('/notifications/events');
    expect(result.events.length).toBeGreaterThan(0);
    for (const event of result.events) {
      expect(event).toHaveProperty('value');
      expect(event).toHaveProperty('label');
      expect(event).toHaveProperty('description');
    }
    const values = result.events.map((e: any) => e.value);
    for (const value of result.defaults) expect(values).toContain(value);
  });

  it('recovery kit and heartbeat endpoints match the contract', async () => {
    const kit = await mockApi.post('/settings/recovery-kit');
    for (const field of ['generated_at', 'server_name', 'arkive_version', 'encryption_password', 'config_keyfile', 'targets', 'instructions', 'text']) {
      expect(kit).toHaveProperty(field);
    }
    for (const field of ['id', 'name', 'type', 'repository', 'host_path', 'details']) {
      expect(kit.targets[0]).toHaveProperty(field);
    }
    const confirmed = await mockApi.post('/settings/recovery-kit/confirm');
    expect(typeof confirmed.confirmed_at).toBe('string');
    const ping = await mockApi.post('/settings/heartbeat/test', { url: 'https://uptime.example.com/api/push/abc' });
    expect(ping).toEqual({ ok: true, status_code: 200, error: null });
  });

  it('settings include the new monitoring, dump and drill keys', async () => {
    const settings = await mockApi.getSettings();
    for (const field of [
      'heartbeat_url', 'stale_backup_hours', 'dump_compression', 'restore_drill_enabled',
      'restore_drill_schedule', 'restore_drill_max_databases', 'restore_drill_max_dump_mb', 'recovery_kit_confirmed_at',
    ]) {
      expect(settings).toHaveProperty(field);
    }
  });

  it('targets response wraps targets in a targets array', async () => {
    const result = await mockApi.listTargets();
    expect(result).toHaveProperty('targets');
    expect(Array.isArray(result.targets)).toBe(true);
    expect(result.targets.length).toBeGreaterThan(0);
  });
});
