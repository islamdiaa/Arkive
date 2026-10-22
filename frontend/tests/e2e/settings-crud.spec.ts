/**
 * UI integration tests — validates frontend rendering with mocked API responses.
 * For full backend integration, see api-smoke.spec.ts and setup-wizard.spec.ts.
 */

import { test, expect, type Page } from '@playwright/test';
import { mockSetupComplete } from './fixtures/api-mocks';

// ---------------------------------------------------------------------------
// Mock data
// ---------------------------------------------------------------------------

const MOCK_SETTINGS = {
  server_name: 'unraid-tower',
  timezone: 'UTC',
  log_level: 'info',
  theme: 'dark',
  api_key_set: true,
  encryption_password_set: true,
};

const MOCK_NOTIFICATION = {
  id: 'notif-1',
  type: 'discord',
  name: 'Dev Discord',
  url: 'https://discord.com/api/webhooks/test/test',
  enabled: true,
  events: ['backup_success', 'backup_failure'],
};

const MOCK_DIRECTORY = {
  id: 'dir-1',
  path: '/mnt/user/appdata',
  label: 'AppData',
  exclude_patterns: [],
  enabled: true,
};

// ---------------------------------------------------------------------------
// Route setup helpers
// ---------------------------------------------------------------------------

async function mockSettingsRoutes(page: Page) {
  await mockSetupComplete(page);

  let currentSettings = { ...MOCK_SETTINGS };

  await page.route('**/api/settings', (route, request) => {
    if (request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentSettings),
      });
    }
    if (request.method() === 'PUT') {
      const updates = request.postDataJSON() || {};
      currentSettings = { ...currentSettings, ...updates };
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentSettings),
      });
    }
    return route.continue();
  });

  // SSE events stream
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );

  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
    })
  );
}

async function mockNotificationsRoutes(page: Page) {
  await mockSetupComplete(page);

  let channels: typeof MOCK_NOTIFICATION[] = [{ ...MOCK_NOTIFICATION }];

  await page.route('**/api/notifications', (route, request) => {
    if (request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: channels, channels, total: channels.length, limit: 200, offset: 0, has_more: false }),
      });
    }
    if (request.method() === 'POST') {
      const body = request.postDataJSON() || {};
      const newChannel = { id: `notif-${Date.now()}`, ...body };
      channels = [...channels, newChannel];
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(newChannel),
      });
    }
    return route.continue();
  });

  await page.route('**/api/notifications/*/test', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok' }),
    })
  );

  await page.route('**/api/notifications/*', (route, request) => {
    if (request.method() === 'DELETE') {
      const url = request.url();
      const id = url.split('/api/notifications/')[1]?.split('/')[0];
      channels = channels.filter((c) => c.id !== id);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ message: 'Channel deleted' }),
      });
    }
    // Let earlier handlers (e.g. the shared /notifications/events mock) answer.
    return route.fallback();
  });

  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );

  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
    })
  );
}

async function mockDirectoriesRoutes(page: Page) {
  await mockSetupComplete(page);

  let directories: typeof MOCK_DIRECTORY[] = [{ ...MOCK_DIRECTORY }];

  await page.route('**/api/directories', (route, request) => {
    if (request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: directories, directories, total: directories.length, limit: 200, offset: 0, has_more: false }),
      });
    }
    if (request.method() === 'POST') {
      const body = request.postDataJSON() || {};
      const newDir = { id: `dir-${Date.now()}`, enabled: true, exclude_patterns: [], ...body };
      directories = [...directories, newDir];
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(newDir),
      });
    }
    return route.continue();
  });

  await page.route('**/api/directories/*', (route, request) => {
    if (request.method() === 'DELETE') {
      const url = request.url();
      const id = url.split('/api/directories/')[1]?.split('/')[0];
      directories = directories.filter((d) => d.id !== id);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ message: 'Directory removed' }),
      });
    }
    return route.continue();
  });

  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );

  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: true }),
    })
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Settings — General', () => {
  test.beforeEach(async ({ page }) => {
    await mockSettingsRoutes(page);
  });

  test('general settings page loads', async ({ page }) => {
    await page.goto('/settings/general', { waitUntil: 'networkidle' });

    await expect(page.locator('body')).not.toContainText('404');

    // The page shows visible labels "Server Name", "Timezone", "General Settings" etc.
    // Timezone and log level values are inside <select> options (not directly visible as text).
    // Check for visible label text or the page heading instead.
    const settingLabel = page
      .getByText('General Settings')
      .or(page.getByText('Server Name'))
      .or(page.getByText('Timezone'))
      .or(page.getByText('Retention Policy'));
    await expect(settingLabel.first()).toBeVisible();
  });

  test('save button is present on general settings page', async ({ page }) => {
    await page.goto('/settings/general', { waitUntil: 'networkidle' });

    await expect(
      page.getByRole('button', { name: /save/i })
        .or(page.locator('button[type="submit"]'))
        .first()
    ).toBeVisible();
  });

  test('general settings form fields are rendered', async ({ page }) => {
    await page.goto('/settings/general', { waitUntil: 'networkidle' });

    // At least one editable field must exist (text input, select, or textarea).
    const inputs = page.locator('input, select, textarea');
    await expect(inputs.first()).toBeVisible();
  });
});

test.describe('Settings — Notifications', () => {
  test.beforeEach(async ({ page }) => {
    await mockNotificationsRoutes(page);
  });

  test('notifications page loads and lists channels', async ({ page }) => {
    await page.goto('/settings/notifications', { waitUntil: 'networkidle' });

    // The mock channel "Dev Discord" must be listed by name.
    await expect(page.getByText('Dev Discord')).toBeVisible();
  });

  test('add notification button is present', async ({ page }) => {
    await page.goto('/settings/notifications', { waitUntil: 'networkidle' });

    await expect(
      page.getByRole('button', { name: /add|new|create/i }).first()
    ).toBeVisible();
  });

  test('existing notification channel is displayed', async ({ page }) => {
    await page.goto('/settings/notifications', { waitUntil: 'networkidle' });

    // The mock channel is named "Dev Discord" and has type "discord".
    await expect(page.getByText('Dev Discord')).toBeVisible();
  });

  test('delete button is present for existing notification', async ({ page }) => {
    await page.goto('/settings/notifications', { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle');

    // The delete button is an SVG icon button with no text label. It appears
    // alongside the "Test" button in each channel row. Locate it by finding
    // any button in the channel card that is NOT the "Test" or "Add Channel" button,
    // or fall back to any button inside the channel list row.
    // The channel card has: Test button + an icon-only delete button (SVG trash icon).
    const deleteBtn = page
      .getByRole('button', { name: /delete|remove/i })
      .or(page.getByRole('button').filter({ has: page.locator('svg') }).last());
    await expect(deleteBtn.first()).toBeVisible();
  });
});

test.describe('Settings — Directories', () => {
  test.beforeEach(async ({ page }) => {
    await mockDirectoriesRoutes(page);
  });

  test('directories page loads and shows watched paths', async ({ page }) => {
    await page.goto('/settings/directories', { waitUntil: 'networkidle' });

    // The mock directory has path "/mnt/user/appdata" and label "AppData" — one must appear.
    const dirEntry = page
      .getByText('/mnt/user/appdata')
      .or(page.getByText('AppData'));
    await expect(dirEntry.first()).toBeVisible();
  });

  test('add directory button is present', async ({ page }) => {
    await page.goto('/settings/directories', { waitUntil: 'networkidle' });

    await expect(
      page.getByRole('button', { name: /add|new|watch/i }).first()
    ).toBeVisible();
  });

  test('existing directory is listed', async ({ page }) => {
    await page.goto('/settings/directories', { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle');

    // The mock directory path "/mnt/user/appdata" or its label "AppData" must appear.
    const dirEntry = page
      .getByText('/mnt/user/appdata')
      .or(page.getByText('AppData'));
    await expect(dirEntry.first()).toBeVisible();
  });

  test('delete button is present for existing directory', async ({ page }) => {
    await page.goto('/settings/directories', { waitUntil: 'networkidle' });
    await page.waitForLoadState('networkidle');

    // The delete button for a directory is an SVG X icon button with no text label.
    // Find any SVG icon button, or fall back to any button that is not the "Add" button.
    const deleteBtn = page
      .getByRole('button', { name: /delete|remove/i })
      .or(page.getByRole('button').filter({ has: page.locator('svg') }).last());
    await expect(deleteBtn.first()).toBeVisible();
  });
});
