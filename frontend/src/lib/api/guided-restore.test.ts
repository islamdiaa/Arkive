import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  confirmationMatches,
  defaultStopSelection,
  isFinished,
  loadPercent,
  methodLabel,
  optionKey,
  replacedLabel,
  statusBadgeClass,
  statusLabel,
} from './guided-restore';
import { mockApi } from './mock';

describe('guided restore helpers', () => {
  it('requires the exact confirmation phrase', () => {
    expect(confirmationMatches('appdb', 'appdb')).toBe(true);
    expect(confirmationMatches('appdb ', 'appdb')).toBe(false);
    expect(confirmationMatches('APPDB', 'appdb')).toBe(false);
    expect(confirmationMatches('', '')).toBe(false);
    expect(confirmationMatches('x', null)).toBe(false);
    expect(confirmationMatches('Immich Library', 'Immich Library')).toBe(true);
  });

  it('maps statuses to badges and labels', () => {
    expect(statusBadgeClass('completed')).toBe('badge-success');
    expect(statusBadgeClass('completed_with_warnings')).toBe('badge-warning');
    expect(statusBadgeClass('failed')).toBe('badge-danger');
    expect(statusBadgeClass('action_required')).toBe('badge-info');
    expect(statusBadgeClass('running')).toContain('animate-status-pulse');
    expect(statusBadgeClass('???')).toContain('badge');
    expect(statusLabel('action_required')).toBe('Action required');
    expect(statusLabel('completed_with_warnings')).toBe('Completed with warnings');
    expect(isFinished('running')).toBe(false);
    expect(isFinished('failed')).toBe(true);
    expect(isFinished(undefined)).toBe(false);
  });

  it('computes load progress only while loading', () => {
    expect(loadPercent({ progress: null })).toBeNull();
    expect(loadPercent({ progress: { bytes_done: 0, bytes_total: 0 } })).toBeNull();
    expect(loadPercent({ progress: { bytes_done: 50, bytes_total: 200 } })).toBe(25);
    expect(loadPercent({ progress: { bytes_done: 500, bytes_total: 200 } })).toBe(100);
  });

  it('suggests stopping only running apps', () => {
    expect(
      defaultStopSelection([
        { name: 'immich-server', status: 'running', reason: 'uses this database' },
        { name: 'immich-ml', status: 'exited', reason: 'same compose project (immich)' },
      ]),
    ).toEqual(['immich-server']);
  });

  it('labels methods and keys options uniquely', () => {
    expect(methodLabel('load')).toBe('Restores in place');
    expect(methodLabel('manual')).toMatch(/by hand/);
    expect(methodLabel('unsupported')).toBe('Not supported');
    const a = optionKey({ container_name: 'app', db_type: 'sqlite', db_name: 'db.sqlite3', host_path: '/a/db.sqlite3' });
    const b = optionKey({ container_name: 'app', db_type: 'sqlite', db_name: 'db.sqlite3', host_path: '/b/db.sqlite3' });
    expect(a).not.toBe(b);
  });
});

describe('guided restore demo mock', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-09T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('serves options with a method for every database', async () => {
    const result: any = await mockApi.get('/restore/database/options');
    expect(result.databases.length).toBeGreaterThan(0);
    for (const option of result.databases) {
      expect(['load', 'manual', 'unsupported']).toContain(option.method);
      expect(Array.isArray(option.dependents)).toBe(true);
    }
    const redis = result.databases.find((d: any) => d.db_type === 'redis');
    expect(redis.method).toBe('manual');
    expect(redis.scope).toMatch(/whole Redis data set/);
    const mongo = result.databases.find((d: any) => d.db_type === 'mongodb');
    expect(mongo.method).toBe('load');
    expect(mongo.scope).toBe('every database on the server except admin, config and local');
    expect(result.databases.find((d: any) => d.db_type === 'postgres').scope).toBeNull();
  });

  it('prepares Redis restores as hand-run steps after checking the container', async () => {
    const selection = { container_name: 'authelia-redis', db_type: 'redis', db_name: 'redis', snapshot_id: 'abc12345', target_id: 'target-b2-main' };
    const preview: any = await mockApi.post('/restore/database/preview', selection);
    expect(preview.method).toBe('manual');
    expect(preview.confirm_phrase).toBeNull();
    expect(preview.steps[0].key).toBe('check_container');
    expect(preview.dump.file.endsWith('.rdb')).toBe(true);
    const started: any = await mockApi.post('/restore/database', { ...selection, confirm: '' });
    vi.setSystemTime(new Date('2026-11-09T12:01:00Z'));
    const op: any = await mockApi.get(`/restore/database/${started.id}`);
    expect(op.status).toBe('action_required');
    const commands = op.instructions.map((i: any) => i.command);
    expect(commands[0]).toBe('docker stop authelia-redis');
    expect(commands.some((c: string) => c.includes('appendonly.aof.manifest'))).toBe(true);
    expect(commands.indexOf('docker start authelia-redis')).toBeGreaterThan(commands.indexOf('docker stop authelia-redis'));
  });

  it('labels what a whole-server MongoDB restore replaces', () => {
    expect(replacedLabel({ db_name: 'admin', scope: 'every database on the server except admin, config and local' })).toMatch(/every database/);
    expect(replacedLabel({ db_name: 'immich', scope: null })).toBe('immich');
  });

  it('previews, runs and undoes a restore like the API', async () => {
    const selection = {
      container_name: 'immich-postgres',
      db_type: 'postgres',
      db_name: 'immich',
      snapshot_id: 'abc12345',
      target_id: 'target-b2-main',
    };
    const preview: any = await mockApi.post('/restore/database/preview', selection);
    for (const field of ['method', 'database', 'snapshot', 'dump', 'dependents', 'confirm_phrase', 'steps', 'blockers', 'warnings']) {
      expect(preview).toHaveProperty(field);
    }
    expect(preview.confirm_phrase).toBe('immich');

    const started: any = await mockApi.post('/restore/database', { ...selection, confirm: 'immich', stop_containers: ['immich-server'] });
    let op: any = await mockApi.get(`/restore/database/${started.id}`);
    expect(op.status).toBe('running');

    vi.setSystemTime(new Date('2026-11-09T12:00:30Z'));
    op = await mockApi.get(`/restore/database/${started.id}`);
    expect(op.status).toBe('completed');
    expect(op.can_undo).toBe(true);
    expect(op.stopped_containers).toEqual(['immich-server']);
    expect(op.steps.every((s: any) => s.status === 'done')).toBe(true);

    const undo: any = await mockApi.post(`/restore/database/${started.id}/undo`, { confirm: 'immich' });
    const undoOp: any = await mockApi.get(`/restore/database/${undo.id}`);
    expect(undoOp.kind).toBe('undo');
    expect(undoOp.parent_id).toBe(started.id);

    const history: any = await mockApi.get('/restore/database?limit=10');
    expect(history.items[0].id).toBe(undo.id);
  });

  it('prepares SQLite restores as hand-run steps', async () => {
    const started: any = await mockApi.post('/restore/database', {
      container_name: 'home-assistant',
      db_type: 'sqlite',
      db_name: 'home-assistant_v2.db',
      host_path: '/mnt/user/appdata/home-assistant/home-assistant_v2.db',
      snapshot_id: 'abc12345',
      target_id: 'target-b2-main',
      confirm: '',
    });
    vi.setSystemTime(new Date('2026-11-09T12:01:00Z'));
    const op: any = await mockApi.get(`/restore/database/${started.id}`);
    expect(op.status).toBe('action_required');
    expect(op.can_undo).toBe(false);
    expect(op.instructions.map((i: any) => i.command)[0]).toBe('docker stop home-assistant');
  });
});
