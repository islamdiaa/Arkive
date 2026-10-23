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
});
