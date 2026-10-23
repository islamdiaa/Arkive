/**
 * Navigation Tests — verify all main routes render without crashing.
 *
 * The frontend is in demo mode (?demo=true → mock API), so setup_completed is
 * true and all pages render with mock data.  This is a valuable test because
 * it confirms the SvelteKit router, component imports, and layout all
 * work correctly.
 */

import { test, expect } from '@playwright/test';
import { stubSseTokenForDemoMode } from './fixtures/api-mocks';

function demoUrl(path: string): string {
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}demo=true`;
}

// Routes to test — each entry is [path, expected heading or content text]
const ROUTES: [string, string][] = [
  ['/', 'Dashboard'],
  ['/backups', 'Backup'],
  ['/databases', 'Database'],
  ['/snapshots', 'Snapshot'],
  ['/restore', 'Restore'],
  ['/activity', 'Activity'],
  ['/logs', 'Log'],
  ['/settings', 'Settings'],
  ['/settings/general', 'General'],
  ['/settings/targets', 'Target'],
  ['/settings/jobs', 'Job'],
  ['/settings/notifications', 'Notification'],
  ['/settings/directories', 'Director'],
  ['/settings/schedule', 'Schedule'],
  ['/settings/security', 'Security'],
];

test.describe('Navigation', () => {
  test.beforeEach(async ({ page }) => {
    // Without this, every demo-mode page is bounced to /login (see
    // stubSseTokenForDemoMode) and these checks would run against the login page.
    await stubSseTokenForDemoMode(page);
  });

  for (const [path, expectedText] of ROUTES) {
    test(`Route ${path} loads and renders content`, async ({ page }) => {
      await page.goto(demoUrl(path), { waitUntil: 'networkidle' });

      // Still on the requested route or one of its sub-pages (/settings opens
      // /settings/general, /restore opens /restore/plan) -- not bounced to
      // /login or /setup.
      const pathname = new URL(page.url()).pathname;
      expect(pathname).not.toMatch(/^\/(login|setup)\b/);
      expect(pathname.startsWith(path)).toBe(true);

      const bodyText = (await page.locator('body').innerText()).toLowerCase();
      expect(bodyText).not.toContain('404');
      expect(bodyText).not.toContain('not found');
      // Expected text (case-insensitive partial match; singular/plural tolerant)
      expect(bodyText).toContain(expectedText.toLowerCase());
    });
  }

  test('Sidebar navigation links are present', async ({ page }) => {
    await page.goto(demoUrl('/'), { waitUntil: 'networkidle' });

    // The sidebar should have navigation links
    // Look for common sidebar items
    const sidebar = page.locator('nav, [class*="sidebar"], aside').first();
    if (await sidebar.isVisible()) {
      const sidebarText = await sidebar.innerText();
      expect(sidebarText.length).toBeGreaterThan(0);
    }
  });

  test('Dashboard shows system status information', async ({ page }) => {
    await page.goto(demoUrl('/'), { waitUntil: 'networkidle' });
    const bodyText = await page.locator('body').innerText();
    // Demo mode should show some status info
    // Check for common dashboard elements
    const hasAnyDashboardContent =
      bodyText.toLowerCase().includes('backup') ||
      bodyText.toLowerCase().includes('status') ||
      bodyText.toLowerCase().includes('dashboard') ||
      bodyText.toLowerCase().includes('arkive');
    expect(hasAnyDashboardContent).toBe(true);
  });
});

test.describe('Navigation — demo mode without a backend session', () => {
  // Regression: the SSE store used to call the real POST /api/auth/sse-token in
  // demo mode, and the 401 (no backend session) redirected to /login.
  test('demo page is not redirected to /login when the SSE token request is rejected', async ({ page }) => {
    await page.route('/api/auth/sse-token', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Not authenticated' }),
      })
    );

    await page.goto(demoUrl('/backups'), { waitUntil: 'networkidle' });

    await expect(page).toHaveURL(/\/backups\?demo=true$/, { timeout: 2_000 });
  });
});
