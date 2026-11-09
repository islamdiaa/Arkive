/**
 * UI integration tests — validates frontend rendering with mocked API responses.
 * For full backend integration, see api-smoke.spec.ts and setup-wizard.spec.ts.
 */

import { test, expect, type Page } from '@playwright/test';
import { mockSetupComplete } from './fixtures/api-mocks';

// ---------------------------------------------------------------------------
// Mock database data
// ---------------------------------------------------------------------------

const mockDatabases = [
  {
    id: 'db-1',
    container_id: 'abc123',
    container_name: 'mariadb',
    type: 'mysql',
    db_type: 'mysql',
    host: '127.0.0.1',
    port: 3306,
    name: 'nextcloud',
    db_name: 'nextcloud',
    status: 'discovered',
    last_dump_at: '2024-06-15T08:00:00Z',
    size_bytes: 10485760,
  },
  {
    id: 'db-2',
    container_id: 'def456',
    container_name: 'postgres',
    type: 'postgresql',
    db_type: 'postgresql',
    host: '127.0.0.1',
    port: 5432,
    name: 'appdb',
    db_name: 'appdb',
    status: 'discovered',
    last_dump_at: null,
    size_bytes: 5242880,
  },
];

// ---------------------------------------------------------------------------
// Route setup helper
// ---------------------------------------------------------------------------

async function mockDatabasesRoutes(page: Page) {
  await mockSetupComplete(page);

  // Databases list
  await page.route('**/api/databases', (route, request) => {
    if (request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          databases: mockDatabases,
          items: mockDatabases,
          total: mockDatabases.length,
        }),
      });
    }
    return route.continue();
  });

  // Discovery scan trigger: the "Rescan" button calls POST /api/discover/scan
  // and then reloads GET /api/databases.
  await page.route('**/api/discover/scan', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total_containers: mockDatabases.length,
        running_containers: mockDatabases.length,
        stopped_containers: 0,
        containers: [],
        databases: mockDatabases,
        flash_config_found: false,
        shares: [],
        scan_duration_seconds: 0.1,
        scanned_at: '2026-03-06T00:00:00Z',
      }),
    })
  );

  // Status endpoint
  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
    })
  );

  // SSE events stream — return empty stream to prevent hanging
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Databases Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockDatabasesRoutes(page);
  });

  test('databases page loads', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    await expect(page.locator('body')).not.toContainText('404');
    await expect(page.locator('body')).not.toContainText('page not found');

    // Layout must be present
    const layout = page.locator('nav, aside, main, [role="main"]');
    await expect(layout.first()).toBeVisible();
  });

  test('discovery scan button is present', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    // A "Scan", "Discover", "Refresh", or "Run Discovery" button must be visible.
    await expect(
      page.getByRole('button', { name: /scan|discover|refresh|run discovery/i }).first()
    ).toBeVisible();
  });

  test('discovered databases list renders', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    // Mock databases have container names "mariadb" and "postgres" — one must appear.
    const dbEntry = page
      .getByText('mariadb')
      .or(page.getByText('postgres'))
      .or(page.getByText('nextcloud'))
      .or(page.getByText('appdb'));
    await expect(dbEntry.first()).toBeVisible();
  });

  test('database list shows container names', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    // Mock databases have container names "mariadb" and "postgres".
    const containerName = page
      .getByText('mariadb')
      .or(page.getByText('postgres'));
    await expect(containerName.first()).toBeVisible();
  });

  test('database list shows database types', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    // Mock databases have types "mysql" and "postgresql" — one must appear.
    const dbType = page
      .getByText('mysql', { exact: false })
      .or(page.getByText('postgresql', { exact: false }));
    await expect(dbType.first()).toBeVisible();
  });

  test('discovery scan button is clickable', async ({ page }) => {
    await page.goto('/databases', { waitUntil: 'networkidle' });

    const scanBtn = page
      .getByRole('button', { name: /scan|discover|refresh|run discovery/i })
      .first();

    // Clicking should trigger a discovery scan and keep the page functional.
    const [response] = await Promise.all([
      page.waitForResponse('**/api/discover/scan'),
      scanBtn.click(),
    ]);
    expect(response.ok()).toBe(true);

    // Page must still render after the click — databases remain listed.
    const dbEntry = page
      .getByText('mariadb')
      .or(page.getByText('postgres'))
      .or(page.getByText('nextcloud'))
      .or(page.getByText('appdb'));
    await expect(dbEntry.first()).toBeVisible();
  });

  test('per-database health is shown and a shrunk size can be accepted', async ({ page }) => {
    const MIB = 1024 * 1024;
    const point = (day: number, mb: number, shrunk = false) => ({
      run_id: `r${day}`,
      at: `2026-11-0${day}T02:00:00Z`,
      status: 'success',
      size_bytes: mb * MIB,
      shrunk,
    });
    const nextcloud = {
      key: 'mariadb_nextcloud-1e7f4a90',
      container_name: 'mariadb',
      db_type: 'mysql',
      db_name: 'nextcloud',
      host_path: null,
      excluded: false,
      status: 'warning',
      issues: ['Latest dump is 80% smaller than its recent median (20.0 MB vs 100.0 MB)'],
      notes: [],
      last_attempt: { status: 'success', at: '2026-11-05T02:00:00Z', run_id: 'r5', error: null },
      last_success_at: '2026-11-05T02:00:00Z',
      last_success_run_id: 'r5',
      size_bytes: 20 * MIB,
      integrity_check: 'ok',
      baseline_bytes: 100 * MIB,
      size_change_percent: -80,
      shrunk: true,
      shrink_percent: 80,
      trend: [point(1, 100), point(2, 100), point(3, 100), point(4, 100), point(5, 20, true)],
      last_drill: {
        status: 'passed',
        at: '2026-11-04T07:00:00Z',
        drill_id: 'd1',
        detail: 'Restored into mariadb:11 — 108 tables',
        dump_file: null,
        dump_dumped_at: '2026-11-03T02:00:00Z',
        snapshot_id: 'abc',
      },
      restorable_as_of: '2026-11-05T02:00:00Z',
    };
    const accepted: string[] = [];
    await page.route('**/api/databases', (route, request) => {
      if (request.method() !== 'GET') return route.continue();
      const items = mockDatabases.map((db) => (db.db_name === 'nextcloud' ? { ...db, key: nextcloud.key } : db));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ databases: items, items, total: items.length }),
      });
    });
    await page.route(/\/api\/databases\/health(\?|$)/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [accepted.length ? { ...nextcloud, status: 'ok', issues: [], shrunk: false } : nextcloud],
          total: 1,
          summary: { ok: 0, warning: 1, failed: 0, never_dumped: 0, excluded: 0 },
          policy: { shrink_alert_percent: 50, shrink_min_mb: 1, baseline_dumps: 5, min_baseline_dumps: 3 },
          latest_upload_at: '2026-11-05T02:10:00Z',
          generated_at: '2026-11-05T03:00:00Z',
        }),
      })
    );
    await page.route(/\/api\/databases\/health\/accept-size$/, async (route, request) => {
      accepted.push(request.postDataJSON().key);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ key: nextcloud.key, accepted_from: nextcloud.last_success_at }),
      });
    });

    await page.goto('/databases', { waitUntil: 'networkidle' });

    const card = page.locator('.card').filter({ hasText: 'nextcloud' });
    const health = card.getByTestId('db-health');
    await expect(health.getByTestId('db-health-status')).toHaveText('Needs attention');
    await expect(health.getByTestId('dump-size-trend')).toBeVisible();
    await expect(health.getByTestId('db-health-size')).toContainText('20 MB');
    await expect(health.getByTestId('db-health-drill')).toContainText('Passed');
    await expect(health).toContainText('80% smaller than its recent median');
    await expect(page.getByTestId('db-health-summary')).toContainText('1 needs attention');

    await health.getByRole('button', { name: 'Accept new size' }).click();

    await expect(health.getByTestId('db-health-status')).toHaveText('Healthy');
    expect(accepted).toEqual([nextcloud.key]);
    // The database without health data still renders as before.
    await expect(page.locator('.card').filter({ hasText: 'appdb' }).getByTestId('db-health')).toHaveCount(0);
  });

  test('a database can be excluded from backups and included again', async ({ page }) => {
    const requests: Array<{ excluded: boolean; db_name: string }> = [];
    await page.route('**/api/databases/exclusions', async (route, request) => {
      const body = request.postDataJSON();
      requests.push(body);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ excluded: body.excluded, database: body, exclusions: [] }),
      });
    });
    await page.goto('/databases', { waitUntil: 'networkidle' });

    const card = page.locator('.card').filter({ hasText: 'nextcloud' });
    await card.getByRole('button', { name: 'Exclude' }).click();
    await expect(card.getByTestId('excluded-badge')).toBeVisible();
    await expect(card.getByRole('button', { name: /dump now/i })).toHaveCount(0);
    await expect(page.getByText(/1 excluded from backups/)).toBeVisible();

    await card.getByRole('button', { name: 'Include' }).click();
    await expect(card.getByTestId('excluded-badge')).toHaveCount(0);
    expect(requests.map((r) => [r.db_name, r.excluded])).toEqual([
      ['nextcloud', true],
      ['nextcloud', false],
    ]);
  });
});
