/**
 * API Smoke Tests — validate every backend endpoint returns valid JSON
 * from a Playwright request context (no browser needed).
 *
 * Tests run sequentially: first check readiness, then perform setup
 * (or handle pre-existing setup from a reused server), then exercise
 * all authenticated CRUD endpoints.
 *
 * IMPORTANT: For a guaranteed clean run, kill existing servers first:
 *   fuser -k -9 5173/tcp 8200/tcp 2>/dev/null; sleep 2
 */

import { test, expect } from '@playwright/test';
import { API, authHeaders } from './helpers';

// Shared state across all tests (serial execution)
let apiKey = '';
let setupWasFresh = false;

test.describe.serial('API Smoke Tests', () => {
  /* ---------------------------------------------------------------- */
  /*  Readiness & no-auth endpoints                                    */
  /* ---------------------------------------------------------------- */

  test('GET /api/status returns 200 with expected fields', async ({ request }) => {
    // The backend may need a moment after the port opens to finish init.
    // request.get() THROWS on connection refused, so we must try/catch.
    let res;
    let body: any;
    for (let attempt = 0; attempt < 30; attempt++) {
      try {
        res = await request.get(`${API}/status`);
        if (res.status() === 200) {
          body = await res.json();
          break;
        }
      } catch {
        // Connection refused — server not ready yet
      }
      await new Promise(r => setTimeout(r, 500));
    }
    expect(res!.status()).toBe(200);
    expect(['ok', 'degraded']).toContain(body.status);
    expect(body).toHaveProperty('version');
    expect(body).toHaveProperty('setup_completed');
    expect(body).toHaveProperty('platform');
    expect(body).toHaveProperty('uptime_seconds');
    expect(typeof body.version).toBe('string');
  });

  /* ---------------------------------------------------------------- */
  /*  Setup (idempotent — handles fresh DB or reused server)           */
  /* ---------------------------------------------------------------- */

  test('POST /api/auth/setup completes and returns API key', async ({ request }) => {
    // Check if setup is already done
    const statusRes = await request.get(`${API}/status`);
    const status = await statusRes.json();

    if (status.setup_completed) {
      // Server was reused from a previous run. Setup is already done.
      // We can't retrieve the old API key, so we'll test without auth
      // for endpoints that allow setup-mode bypass, or skip auth tests.
      // Since we DO have a setup-completed server, attempt to POST setup
      // and verify it returns 409 (duplicate).
      const dupRes = await request.post(`${API}/auth/setup`, {
        data: { encryption_password: 'test-password-12chars' },
      });
      expect(dupRes.status()).toBe(409);
      setupWasFresh = false;
      // We can't run authenticated tests without the API key.
      // Mark apiKey as empty — authenticated tests will be skipped.
      apiKey = '';
    } else {
      // Fresh database — perform setup
      setupWasFresh = true;
      const res = await request.post(`${API}/auth/setup`, {
        data: {
          encryption_password: 'test-password-12chars',
          db_dump_schedule: '0 6,18 * * *',
          cloud_sync_schedule: '0 7 * * *',
          flash_schedule: '0 6 * * *',
          directories: [],
          run_first_backup: false,
        },
      });
      expect([200, 201]).toContain(res.status());
      const body = await res.json();
      expect(body).toHaveProperty('api_key');
      expect(body.api_key.length).toBeGreaterThan(10);
      expect(body).toHaveProperty('message');
      apiKey = body.api_key;
    }
  });

  test('POST /api/auth/setup rejects duplicate setup with 409', async ({ request }) => {
    const res = await request.post(`${API}/auth/setup`, {
      data: { encryption_password: 'another-password-12chars' },
    });
    expect(res.status()).toBe(409);
  });

  test('GET /api/status shows setup_completed = true', async ({ request }) => {
    const res = await request.get(`${API}/status`);
    const body = await res.json();
    expect(body.setup_completed).toBe(true);
  });

  /* ---------------------------------------------------------------- */
  /*  Auth enforcement                                                 */
  /* ---------------------------------------------------------------- */

  test('Authenticated endpoints reject missing API key', async ({ request }) => {
    const res = await request.get(`${API}/jobs`);
    // May return 401 (unauthorized) or 429 (rate-limited from prior failed attempts)
    expect([401, 429]).toContain(res.status());
  });

  test('Authenticated endpoints reject invalid API key', async ({ request }) => {
    const res = await request.get(`${API}/jobs`, {
      headers: { 'X-API-Key': 'invalid-key-that-should-fail' },
    });
    // May return 401 (unauthorized) or 429 (rate-limited from prior failed attempts)
    expect([401, 429]).toContain(res.status());
  });

  /* ---------------------------------------------------------------- */
  /*  Authenticated CRUD (requires valid API key from fresh setup)     */
  /* ---------------------------------------------------------------- */

  test('GET /api/jobs lists jobs', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key (server was reused from previous run)');
    const res = await request.get(`${API}/jobs`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('jobs');
    expect(Array.isArray(body.jobs)).toBe(true);
    // Setup creates 3 default jobs
    expect(body.jobs.length).toBe(3);
  });

  test('POST /api/jobs creates a new job', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.post(`${API}/jobs`, {
      headers: authHeaders(apiKey),
      data: {
        name: 'Test Job',
        type: 'full',
        schedule: '0 4 * * *',
        directories: [],
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body).toHaveProperty('id');
    expect(body).toHaveProperty('name', 'Test Job');
  });

  test('GET /api/jobs now includes the new job (4 total)', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/jobs`, {
      headers: authHeaders(apiKey),
    });
    const body = await res.json();
    expect(body.jobs.length).toBe(4);
    expect(body.jobs.some((j: any) => j.name === 'Test Job')).toBe(true);
  });

  test('DELETE /api/jobs/:id deletes the test job', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const listRes = await request.get(`${API}/jobs`, {
      headers: authHeaders(apiKey),
    });
    const jobs = (await listRes.json()).jobs;
    const testJob = jobs.find((j: any) => j.name === 'Test Job');
    expect(testJob).toBeTruthy();

    const delRes = await request.delete(`${API}/jobs/${testJob.id}`, {
      headers: authHeaders(apiKey),
    });
    expect(delRes.status()).toBe(200);

    // Verify deletion
    const afterRes = await request.get(`${API}/jobs`, {
      headers: authHeaders(apiKey),
    });
    expect((await afterRes.json()).jobs.length).toBe(3);
  });

  /* -- Targets -- */

  test('GET /api/targets returns list', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/targets`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('targets');
    expect(Array.isArray(body.targets)).toBe(true);
  });

  /* -- Settings -- */

  test('GET /api/settings returns default values', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/settings`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('timezone');
    expect(body).toHaveProperty('log_level');
    expect(body).toHaveProperty('theme');
    expect(body.api_key_set).toBe(true);
    expect(body.encryption_password_set).toBe(true);
  });

  test('PUT /api/settings updates server_name', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.put(`${API}/settings`, {
      headers: authHeaders(apiKey),
      data: { server_name: 'test-tower' },
    });
    expect(res.status()).toBe(200);

    const getRes = await request.get(`${API}/settings`, {
      headers: authHeaders(apiKey),
    });
    expect((await getRes.json()).server_name).toBe('test-tower');
  });

  /* -- Notifications -- */

  test('GET /api/notifications returns list', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/notifications`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('channels');
    expect(Array.isArray(body.channels)).toBe(true);
  });

  test('POST /api/notifications creates a channel', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.post(`${API}/notifications`, {
      headers: authHeaders(apiKey),
      data: {
        type: 'discord',
        name: 'Test Discord',
        url: 'https://discord.com/api/webhooks/test/test',
        enabled: true,
        events: ['backup.success', 'backup.failed'],
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body).toHaveProperty('id');
    expect(body).toHaveProperty('name', 'Test Discord');
  });

  test('POST /api/notifications rejects unknown event types', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.post(`${API}/notifications`, {
      headers: authHeaders(apiKey),
      data: {
        type: 'discord',
        name: 'Bad Events',
        url: 'https://discord.com/api/webhooks/test/test',
        events: ['backup_success'],
      },
    });
    expect(res.status()).toBe(422);
  });

  test('GET /api/notifications/events lists the event catalog', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/notifications/events`, { headers: authHeaders(apiKey) });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.events.map((e: any) => e.value)).toContain('backup.stale');
  });

  test('DELETE /api/notifications/:id deletes a channel', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const listRes = await request.get(`${API}/notifications`, {
      headers: authHeaders(apiKey),
    });
    const channels = (await listRes.json()).channels;
    const ch = channels.find((c: any) => c.name === 'Test Discord');
    expect(ch).toBeTruthy();

    const delRes = await request.delete(`${API}/notifications/${ch.id}`, {
      headers: authHeaders(apiKey),
    });
    expect(delRes.status()).toBe(200);
  });

  /* -- Directories -- */

  test('GET /api/directories returns list', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/directories`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('directories');
    expect(Array.isArray(body.directories)).toBe(true);
  });

  test('POST /api/directories creates a watched directory', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.post(`${API}/directories`, {
      headers: authHeaders(apiKey),
      data: {
        path: '/tmp',
        label: 'Temp Dir',
        exclude_patterns: ['*.log'],
        enabled: true,
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body).toHaveProperty('id');
    expect(body).toHaveProperty('path', '/tmp');
  });

  test('DELETE /api/directories/:id removes a directory', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const listRes = await request.get(`${API}/directories`, {
      headers: authHeaders(apiKey),
    });
    const dirs = (await listRes.json()).directories;
    const dir = dirs.find((d: any) => d.label === 'Temp Dir');
    expect(dir).toBeTruthy();

    const delRes = await request.delete(`${API}/directories/${dir.id}`, {
      headers: authHeaders(apiKey),
    });
    expect(delRes.status()).toBe(200);
  });

  /* -- Activity -- */

  test('GET /api/activity returns valid JSON', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/activity`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('activities');
  });

  /* -- Logs -- */

  test('GET /api/logs returns valid JSON', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/logs`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('logs');
    expect(body).toHaveProperty('total');
  });

  /* -- Storage -- */

  test('GET /api/storage returns valid JSON', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/storage`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(typeof body).toBe('object');
  });

  /* -- Snapshots -- */

  test('GET /api/snapshots returns valid JSON', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/snapshots`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('snapshots');
  });

  /* -- Databases -- */

  test('GET /api/databases returns valid JSON', async ({ request }) => {
    test.skip(!apiKey, 'Skipped: no API key');
    const res = await request.get(`${API}/databases`, {
      headers: authHeaders(apiKey),
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(typeof body).toBe('object');
  });
});
