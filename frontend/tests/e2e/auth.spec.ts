import { test, expect } from '@playwright/test';
import { mockDashboardData, mockSetupComplete } from './fixtures/api-mocks';

test.describe('Authentication', () => {
  test('redirects to /setup when setup has not been completed', async ({ page }) => {
    await page.route('**/api/auth/session', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ setup_required: true, authenticated: false }),
      })
    );
    await page.route('**/api/status', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: false }),
      })
    );
    await page.route('**/api/events/**', (route) =>
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    );
    await page.goto('/');
    await expect(page).toHaveURL(/setup/);
  });

  test('redirects to /login when setup is complete but no browser session exists', async ({ page }) => {
    await page.route('**/api/auth/session', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          setup_required: false,
          authenticated: false,
          setup_completed_at: '2026-03-06T00:00:00Z',
        }),
      })
    );
    await page.route('**/api/events/**', (route) =>
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    );

    await page.goto('/');
    await expect(page).toHaveURL(/login/);
  });

  test('loads dashboard when authenticated', async ({ page }) => {
    await page.route('**/api/auth/session', (route) =>
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
    await page.route('**/api/status', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
      })
    );
    await page.route('**/api/events/**', (route) =>
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    );
    await page.route('**/api/**', (route) => {
      if (route.request().url().includes('/status') || route.request().url().includes('/session') || route.request().url().includes('/events'))
        return;
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });
    await page.goto('/');
    // Should not redirect to setup
    await expect(page).not.toHaveURL(/setup/);
    await expect(page).not.toHaveURL(/login/);
  });

  // The root layout hides the sidebar on /login with a `$:` statement on the
  // route ($lib/stores/page). Leaving /login without a reload must bring it back.
  test('leaving /login by client-side navigation shows the sidebar', async ({ page }) => {
    // Backend API only: a '**/api/**' glob would also catch the dev server's
    // /src/lib/api/*.ts modules.
    await page.route(
      (url) => url.pathname.startsWith('/api/'),
      (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    );
    await mockSetupComplete(page);
    await mockDashboardData(page);
    await page.route('**/api/status', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
      })
    );
    await page.route('**/api/events/**', (route) =>
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    );

    // Already signed in, so the login page forwards to ?next= with goto().
    await page.goto('/login?next=/backups');

    await expect(page).toHaveURL(/:\d+\/backups$/);
    await expect(page.locator('aside')).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toContainText('Backups');
  });

  // SvelteKit 3's goto() rejects a path that matches no route. The login page
  // must still leave /login for such a ?next= (and show the 404 page there).
  test('login with a ?next= path that matches no route lands on the 404 page', async ({ page }) => {
    await mockSetupComplete(page);
    await page.route('**/api/status', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
      })
    );
    await page.route('**/api/events/**', (route) =>
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    );

    await page.goto('/login?next=/no-such-page');

    // Anchored on the port: /login?next=/no-such-page must not match.
    await expect(page).toHaveURL(/:\d+\/no-such-page$/);
    await expect(page.getByRole('heading', { name: 'Page Not Found' })).toBeVisible();
  });

  // Browsers drop tab/CR/LF and read '\' as '/' while parsing a URL, so each
  // of these ?next= values means //evil.example. goto() rejects it as external;
  // the login page must fall back to the dashboard, never load it.
  for (const next of ['/%09/evil.example/phish', '/%0A/evil.example', '/%0D%5Cevil.example']) {
    test(`login with ?next=${next} stays on this origin`, async ({ page }) => {
      const offsite: string[] = [];
      await page.route(
        (url) => url.hostname === 'evil.example',
        (route) => {
          offsite.push(route.request().url());
          return route.abort();
        }
      );
      await page.route(
        (url) => url.pathname.startsWith('/api/'),
        (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      );
      await mockSetupComplete(page);
      await mockDashboardData(page);
      await page.route('**/api/status', (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
        })
      );
      await page.route('**/api/events/**', (route) =>
        route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      );

      // Already signed in, so the login page forwards to ?next= straight away.
      await page.goto(`/login?next=${next}`);

      await expect(page).toHaveURL(/^http:\/\/127\.0\.0\.1:\d+\/$/);
      expect(offsite).toEqual([]);
    });
  }
});
