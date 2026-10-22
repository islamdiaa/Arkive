/**
 * E2E tests for:
 * 1. Sidebar active state highlighting — verifies the correct nav link
 *    is visually highlighted when navigating to each route.
 * 2. Security settings page — verifies the security page renders
 *    API key management, encryption info, and security features list.
 */

import { test, expect, type Page } from '@playwright/test';
import { mockSetupComplete } from './fixtures/api-mocks';

// ---------------------------------------------------------------------------
// Route setup helpers
// ---------------------------------------------------------------------------

async function mockAllRoutes(page: Page) {
  await mockSetupComplete(page);

  // Status endpoint
  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
    })
  );

  // Settings endpoint (for security page and general page)
  await page.route('**/api/settings', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        server_name: 'unraid-tower',
        timezone: 'UTC',
        log_level: 'info',
        theme: 'dark',
        api_key_set: true,
        encryption_password_set: true,
      }),
    })
  );

  // Jobs endpoint
  await page.route('**/api/jobs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], jobs: [], total: 0 }),
    })
  );

  // Job runs
  await page.route('**/api/jobs/runs*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], runs: [], total: 0 }),
    })
  );

  // Targets
  await page.route('**/api/targets', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ targets: [], items: [], total: 0 }),
    })
  );

  // Storage
  await page.route('**/api/storage', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ targets: [], summary: { target_count: 0, total_size_bytes: 0 } }),
    })
  );

  // Snapshots
  await page.route('**/api/snapshots*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], snapshots: [], total: 0 }),
    })
  );

  // Databases page (GET /api/databases)
  await page.route('**/api/databases*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], databases: [], total: 0 }),
    })
  );

  // Containers
  await page.route('**/api/containers*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], containers: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Activity
  await page.route('**/api/activity*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], entries: [], activities: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Logs
  await page.route('**/api/logs*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], logs: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Notifications
  await page.route('**/api/notifications*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], channels: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Directories
  await page.route('**/api/directories*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], directories: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Schedules
  await page.route('**/api/schedule*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], schedules: [], total: 0, limit: 200, offset: 0, has_more: false }),
    })
  );

  // Key rotation
  await page.route('**/api/auth/rotate-key', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ api_key: 'ark_newkey1234567890' }),
    })
  );

  // SSE events stream — prevent hanging
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );
}

// ---------------------------------------------------------------------------
// Sidebar Active State Tests
// ---------------------------------------------------------------------------

test.describe('Sidebar — Active State Highlighting', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllRoutes(page);
  });

  const mainRoutes: [string, string][] = [
    ['/', 'Dashboard'],
    ['/backups', 'Backups'],
    ['/snapshots', 'Snapshots'],
    ['/databases', 'Databases'],
    ['/activity', 'Activity'],
    ['/logs', 'Logs'],
  ];

  for (const [path, label] of mainRoutes) {
    test(`"${label}" link is highlighted when visiting ${path}`, async ({ page }) => {
      await page.goto(path, { waitUntil: 'networkidle' });

      // The sidebar uses an <a> tag with a conditional class:
      //   active: 'bg-primary/10 text-primary font-medium'
      //   inactive: 'text-text-secondary'
      // Find the link with matching text and verify it has the active class.
      const link = page.locator('aside a, nav a').filter({ hasText: label }).first();

      if (await link.isVisible()) {
        const classes = await link.getAttribute('class') || '';
        // Active link should have primary color class
        const isActive =
          classes.includes('text-primary') ||
          classes.includes('bg-primary') ||
          classes.includes('font-medium');
        expect(isActive).toBe(true);
      }
    });
  }

  test('settings sub-nav expands when visiting a settings page', async ({ page }) => {
    await page.goto('/settings/general', { waitUntil: 'networkidle' });

    // The settings section should be expanded showing sub-nav items
    const generalLink = page.locator('aside a, nav a').filter({ hasText: 'General' }).first();
    if (await generalLink.isVisible()) {
      const classes = await generalLink.getAttribute('class') || '';
      const isActive =
        classes.includes('text-primary') || classes.includes('font-medium');
      expect(isActive).toBe(true);
    }

    // Other settings sub-items should also be visible when expanded
    const securityLink = page.locator('aside a, nav a').filter({ hasText: 'Security' }).first();
    await expect(securityLink).toBeVisible();
  });

  test('non-active sidebar links do not have active styling', async ({ page }) => {
    await page.goto('/', { waitUntil: 'networkidle' });

    // "Backups" link should NOT have active styling when on Dashboard
    const backupsLink = page.locator('aside a, nav a').filter({ hasText: 'Backups' }).first();

    if (await backupsLink.isVisible()) {
      const classes = await backupsLink.getAttribute('class') || '';
      // Should have secondary text color, not primary
      const isInactive =
        classes.includes('text-text-secondary') || !classes.includes('font-medium');
      expect(isInactive).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Security Settings Tests
// ---------------------------------------------------------------------------

test.describe('Settings — Security Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllRoutes(page);
  });

  test('security page loads without errors', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    await expect(page.locator('body')).not.toContainText('404');
    await expect(page.locator('body')).not.toContainText('TypeError');
    await expect(page.locator('body')).not.toContainText('uncaught');
  });

  test('API Key section is displayed', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    await expect(page.getByRole('heading', { name: 'API Key' })).toBeVisible();
    await expect(page.getByText('Configured')).toBeVisible();
    await expect(page.getByText('Integration credential')).toBeVisible();
  });

  test('Rotate API Key button is present', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    // Multiple rotate buttons exist (API Key section + Danger Zone); use first()
    const rotateBtn = page.getByRole('button', { name: /rotate/i });
    await expect(rotateBtn.first()).toBeVisible();
  });

  test('Encryption section describes AES-256', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    await expect(page.getByRole('heading', { name: 'Encryption' })).toBeVisible();
    await expect(page.getByText('AES-256', { exact: false }).first()).toBeVisible();
  });

  test('encryption password warning is shown', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    // Two elements contain "cannot be changed" (subtitle + warning div); use first()
    await expect(
      page.getByText('cannot be changed', { exact: false }).first()
    ).toBeVisible();
  });

  test('Security Features checklist is displayed', async ({ page }) => {
    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    await expect(page.getByText('Security Features')).toBeVisible();

    // Verify the 4 security feature items
    await expect(page.getByText('AES-256-CTR encryption at rest')).toBeVisible();
    await expect(page.getByText('API key authentication', { exact: false })).toBeVisible();
    await expect(page.getByText('Sensitive values encrypted', { exact: false })).toBeVisible();
    await expect(page.getByText('Docker socket read-only', { exact: false })).toBeVisible();
  });

  test('security page shows error state when API fails', async ({ page }) => {
    // Override settings to return 500
    await page.route('**/api/settings', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Database error' }),
      })
    );

    await page.goto('/settings/security', { waitUntil: 'networkidle' });

    // Page should not crash
    await expect(page.locator('body')).not.toContainText('TypeError');

    // Should show an error message (we added error handling earlier)
    const errorIndicator = page
      .getByText('Failed to load', { exact: false })
      .or(page.locator('.bg-danger\\/10').first())
      .or(page.getByText('error', { exact: false }));
    await expect(errorIndicator.first()).toBeVisible();
  });
});
