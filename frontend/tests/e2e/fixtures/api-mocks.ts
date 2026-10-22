import type { Page } from '@playwright/test';

/** Mock the setup completion state so pages load normally. */
export async function mockSetupComplete(page: Page, apiKey = 'ark_test1234') {
  await page.route('/api/auth/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        setup_required: false,
        authenticated: true,
        setup_completed_at: '2026-03-06T00:00:00Z',
      }),
    })
  );
  // The layout opens the SSE stream with a short-lived token from
  // POST /api/auth/sse-token. Answer it consistently with the mocked session:
  // left to the real backend it returns 401, the app's global 401 handling
  // sends the browser to /login, and /login bounces straight back because the
  // mocked session says "authenticated" -- a redirect cycle that races (or
  // never lets the page reach) the assertions.
  await page.route('/api/auth/sse-token', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'e2e-sse-token' }),
    })
  );
  await mockBackgroundEndpoints(page);
}

/**
 * Endpoints that components fetch on their own (drills panel, notification
 * event picker, cron preview). Registered first, so a spec's own routes —
 * added later — take precedence; anything unmocked would reach the real
 * backend, get a 401 and bounce the page to /login.
 */
export async function mockBackgroundEndpoints(page: Page) {
  // A regex, not '**/api/drills**': that glob also matches the dev server's
  // module URL /src/lib/api/drills.ts and would serve JSON as JavaScript.
  await page.route(/\/api\/drills(\/|\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 }),
    })
  );
  await page.route('**/api/notifications/events', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        events: [
          { value: 'backup.success', label: 'Backup succeeded', description: '' },
          { value: 'backup.partial', label: 'Backup incomplete', description: '' },
          { value: 'backup.failed', label: 'Backup failed', description: '' },
          { value: 'backup.stale', label: 'Backups overdue', description: '' },
          { value: 'verification.failed', label: 'Verification failed', description: '' },
          { value: 'drill.failed', label: 'Restore drill failed', description: '' },
          { value: 'target.error', label: 'Storage target unreachable', description: '' },
          { value: 'system.startup', label: 'Arkive started', description: '' },
          { value: 'system.shutdown', label: 'Arkive stopped', description: '' },
        ],
        defaults: ['backup.failed', 'backup.partial', 'backup.stale', 'verification.failed',
          'drill.failed', 'target.error'],
      }),
    })
  );
  await page.route(/\/api\/settings\/cron-preview(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ next_runs: ['2026-03-08T07:00:00Z', '2026-03-15T07:00:00Z'] }),
    })
  );
}

/** Mock an empty dashboard state. */
export async function mockDashboardData(page: Page) {
  await page.route('/api/jobs/runs?*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 }),
    })
  );
  await page.route('/api/storage', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ targets: [], summary: { target_count: 0, total_size_bytes: 0 } }),
    })
  );
  await page.route('/api/jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 }),
    })
  );
}

/** Mock setup-required state so the app acts like first-run. */
export async function mockSetupRequired(page: Page) {
  await page.route('**/api/auth/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ setup_required: true }),
    })
  );
  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: false }),
    })
  );
}

/** Mock the jobs list with sample data. */
export async function mockJobsList(page: Page) {
  await page.route('/api/jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          { id: 'j1', name: 'DB Dumps', type: 'db_dump', schedule: '0 1 * * *', enabled: true },
          { id: 'j2', name: 'Cloud Sync', type: 'full', schedule: '0 2 * * *', enabled: true },
          { id: 'j3', name: 'Flash Backup', type: 'flash', schedule: '0 3 * * *', enabled: true },
        ],
        total: 3,
      }),
    })
  );
}

/**
 * Keep demo mode (?demo=true) on the page a spec navigates to.
 *
 * Known app bug: in demo mode the API client serves mock data (including an
 * authenticated session), but the SSE store still asks the real backend for
 * POST /api/auth/sse-token. Without a backend session that is a 401, which the
 * global 401 handler treats as an expired session and sends the browser to
 * /login (dropping ?demo=true). Demo-mode specs stub the token so they test the
 * page they opened; navigation.spec.ts keeps a test.fail() case reproducing it.
 */
export async function stubSseTokenForDemoMode(page: Page) {
  await page.route('/api/auth/sse-token', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'e2e-sse-token' }),
    })
  );
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );
}
