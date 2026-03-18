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

  it('targets response wraps targets in a targets array', async () => {
    const result = await mockApi.listTargets();
    expect(result).toHaveProperty('targets');
    expect(Array.isArray(result.targets)).toBe(true);
    expect(result.targets.length).toBeGreaterThan(0);
  });
});
