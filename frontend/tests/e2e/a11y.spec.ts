/**
 * Accessibility checks: axe-core (WCAG 2.2 A/AA plus axe best practices) on
 * every main route, in both themes, at desktop width and at 375px; no
 * horizontal page scroll on phones; and keyboard behaviour of the skip link,
 * the mobile sidebar, the modal dialogs and the command palette.
 *
 * Pages run in demo mode (?demo=true, mock API). Every /api/ request is
 * answered inside the browser, so these tests never depend on, or reach,
 * a backend.
 */

import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'];

const DESKTOP = { width: 1280, height: 800 };
const PHONE = { width: 375, height: 812 };

// [path, label]. Paths with ids use records from the demo mock.
const APP_ROUTES: [string, string][] = [
  ['/', 'dashboard'],
  ['/activity', 'activity'],
  ['/backups', 'backups'],
  ['/backups/run-demo-1', 'backup run detail'],
  ['/databases', 'databases'],
  ['/snapshots', 'snapshots'],
  ['/snapshots/abc12345', 'snapshot detail'],
  ['/restore/plan', 'restore plan'],
  ['/restore/browse', 'restore browse'],
  ['/restore/database', 'restore database'],
  ['/logs', 'logs'],
  ['/settings/general', 'settings: general'],
  ['/settings/targets', 'settings: targets'],
  ['/settings/schedule', 'settings: schedule'],
  ['/settings/jobs', 'settings: jobs'],
  ['/settings/directories', 'settings: directories'],
  ['/settings/notifications', 'settings: notifications'],
  ['/settings/monitoring', 'settings: monitoring'],
  ['/settings/security', 'settings: security'],
  ['/settings/recovery-kit', 'settings: recovery kit'],
];

// Routes whose data arrives after first paint without a skeleton. Wait for it,
// so axe checks the rendered page rather than a loading message.
const READY: Record<string, (page: Page) => Promise<void>> = {
  '/logs': async (page) => {
    const entries = page.getByRole('region', { name: 'Log entries' });
    await expect(entries).toContainText('Starting daily backup run');
    await expect(entries).not.toContainText('Loading logs');
  },
};

function demoUrl(path: string): string {
  return `${path}${path.includes('?') ? '&' : '?'}demo=true`;
}

function json(body: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/**
 * Answer every /api/ request in the browser. Registered first, so the more
 * specific routes added after it win. A regex on the path, not a glob:
 * '** /api/**' would also match the dev server's /src/lib/api/*.ts modules.
 */
async function isolateFromBackend(page: Page) {
  await page.route(/^https?:\/\/[^/]+\/api\//, (route) => {
    const url = route.request().url();
    if (url.includes('/api/events/')) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    }
    return route.fulfill(json({ message: 'not mocked in a11y.spec.ts' }, 404));
  });
  // Demo mode still asks the real backend for an SSE token (see
  // stubSseTokenForDemoMode in fixtures/api-mocks.ts).
  await page.route('**/api/auth/sse-token', (route) => route.fulfill(json({ token: 'a11y-sse-token' })));
}

async function setTheme(page: Page, theme: 'dark' | 'light') {
  await page.evaluate((t) => {
    document.documentElement.classList.toggle('dark', t === 'dark');
    document.documentElement.classList.toggle('light', t === 'light');
  }, theme);
}

/**
 * Jump running animations to their end state. Svelte transitions (the page
 * fly-in, modal fades) use the Web Animations API, which the stylesheet in
 * settle() does not stop; a half-faded element would make contrast results
 * depend on timing.
 */
async function finishAnimations(page: Page) {
  await page.evaluate(() => {
    for (const animation of document.getAnimations()) {
      try {
        animation.finish();
      } catch {
        animation.cancel(); // infinite animations cannot finish
      }
    }
  });
}

/** Wait for the page's data to render and freeze animations so contrast is stable. */
async function settle(page: Page) {
  await expect(page.locator('main').first()).toBeVisible();
  await expect(page.locator('.animate-skeleton')).toHaveCount(0);
  await page.addStyleTag({
    content: '*, *::before, *::after { animation: none !important; transition: none !important; }',
  });
  await finishAnimations(page);
}

function formatViolations(violations: Awaited<ReturnType<AxeBuilder['analyze']>>['violations']): string {
  return violations
    .map((v) => {
      const nodes = v.nodes
        .slice(0, 5)
        .map((n) => `      ${n.target.join(' ')}\n        ${(n.failureSummary ?? '').split('\n').slice(1).join(' ').trim()}`)
        .join('\n');
      return `  [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} node(s))\n${nodes}`;
    })
    .join('\n');
}

async function expectNoAxeViolations(page: Page, context: string, include?: string) {
  await finishAnimations(page);
  let builder = new AxeBuilder({ page }).withTags(AXE_TAGS);
  if (include) builder = builder.include(include);
  const { violations } = await builder.analyze();
  expect(violations, `axe violations (${context}):\n${formatViolations(violations)}`).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const { scrollWidth, clientWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(scrollWidth, 'page scrolls horizontally').toBeLessThanOrEqual(clientWidth);
}

test.use({ contextOptions: { reducedMotion: 'reduce' } });

test.describe('axe: app routes', () => {
  test.beforeEach(async ({ page }) => {
    await isolateFromBackend(page);
  });

  for (const [path, label] of APP_ROUTES) {
    test(`${label} (${path}) at desktop width, dark and light`, async ({ page }) => {
      await page.setViewportSize(DESKTOP);
      await page.goto(demoUrl(path), { waitUntil: 'networkidle' });
      expect(new URL(page.url()).pathname).toBe(path);
      await READY[path]?.(page);
      await settle(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `${path} desktop ${theme}`);
      }
    });

    test(`${label} (${path}) at 375px, dark and light`, async ({ page }) => {
      await page.setViewportSize(PHONE);
      await page.goto(demoUrl(path), { waitUntil: 'networkidle' });
      expect(new URL(page.url()).pathname).toBe(path);
      await READY[path]?.(page);
      await settle(page);
      await expectNoHorizontalOverflow(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `${path} 375px ${theme}`);
      }
    });
  }
});

test.describe('axe: pages outside the app shell', () => {
  test.beforeEach(async ({ page }) => {
    await isolateFromBackend(page);
  });

  test('login page, both widths and themes', async ({ page }) => {
    await page.route('**/api/auth/session', (route) =>
      route.fulfill(json({ setup_required: false, authenticated: false, setup_completed_at: '2026-03-06T00:00:00Z' })),
    );
    for (const viewport of [DESKTOP, PHONE]) {
      await page.setViewportSize(viewport);
      await page.goto('/login', { waitUntil: 'networkidle' });
      await settle(page);
      await expectNoHorizontalOverflow(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `/login ${viewport.width}px ${theme}`);
      }
    }
  });

  test('setup wizard first step, both widths and themes', async ({ page }) => {
    await page.route('**/api/auth/session', (route) => route.fulfill(json({ setup_required: true })));
    await page.route('**/api/status', (route) =>
      route.fulfill(json({ version: '0.4.0', status: 'ok', setup_completed: false })),
    );
    for (const viewport of [DESKTOP, PHONE]) {
      await page.setViewportSize(viewport);
      await page.goto('/setup', { waitUntil: 'networkidle' });
      await settle(page);
      await expectNoHorizontalOverflow(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `/setup ${viewport.width}px ${theme}`);
      }
    }
  });

  test('setup wizard, every step through a rejected storage target, both widths and themes', async ({ page }) => {
    test.setTimeout(90_000);
    await page.route('**/api/auth/session', (route) => route.fulfill(json({ setup_required: true })));
    await page.route('**/api/status', (route) =>
      route.fulfill(json({ version: '0.4.0', status: 'ok', setup_completed: false })),
    );
    // Unraid with appdata present: step 5 shows the "preselected appdata" hint.
    await page.route('**/api/directories/scan', (route) =>
      route.fulfill(json({ platform: 'unraid', directories: [{ path: '/mnt/user/appdata' }], suggestions: [] })),
    );
    // The backend rejected the target: step 6 shows the "NOT saved" alert.
    await page.route('**/api/auth/setup', (route) =>
      route.fulfill(
        json({
          api_key: 'ark_a11y_test_key',
          target_created: false,
          target_errors: ['Local path is not on a mounted volume'],
          setup_completed_at: '2026-03-06T00:00:00Z',
        }),
      ),
    );

    async function auditStep(name: string) {
      for (const viewport of [DESKTOP, PHONE]) {
        await page.setViewportSize(viewport);
        await settle(page);
        await expectNoHorizontalOverflow(page);
        for (const theme of ['dark', 'light'] as const) {
          await setTheme(page, theme);
          await expectNoAxeViolations(page, `/setup ${name} ${viewport.width}px ${theme}`);
        }
      }
    }

    await page.goto('/setup', { waitUntil: 'networkidle' });
    await expect(page.getByRole('heading', { name: 'Encryption Password' })).toBeVisible();
    await page.locator('#enc-password').fill('a11y-test-passphrase');
    await page.locator('#confirm-password').fill('a11y-test-passphrase');
    await auditStep('step 1');

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Container Discovery' })).toBeVisible();
    await auditStep('step 2');

    await page.getByRole('button', { name: 'Skip' }).click();
    await expect(page.getByRole('heading', { name: 'Backup Destination' })).toBeVisible();
    await page.getByRole('button', { name: /Local Path/ }).click();
    await auditStep('step 3');

    await page.getByRole('button', { name: 'Next' }).click();
    await auditStep('step 4');

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Directories to Watch' })).toBeVisible();
    await expect(page.getByText('Arkive preselected /mnt/user/appdata')).toBeVisible();
    await auditStep('step 5 (Unraid appdata hint)');

    await page.getByRole('button', { name: 'Complete Setup' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'storage destination was NOT saved' })).toBeVisible();
    await auditStep('step 6 (target rejected)');
  });

  test('setup wizard directories step, Unraid without appdata (warning hint), both widths and themes', async ({ page }) => {
    await page.route('**/api/auth/session', (route) => route.fulfill(json({ setup_required: true })));
    await page.route('**/api/status', (route) =>
      route.fulfill(json({ version: '0.4.0', status: 'ok', setup_completed: false })),
    );
    await page.route('**/api/directories/scan', (route) =>
      route.fulfill(json({ platform: 'unraid', directories: [], suggestions: [] })),
    );
    // The wizard resumes at the step saved in sessionStorage.
    await page.addInitScript(() => sessionStorage.setItem('arkive_setup_step', '5'));
    for (const viewport of [DESKTOP, PHONE]) {
      await page.setViewportSize(viewport);
      await page.goto('/setup', { waitUntil: 'networkidle' });
      await expect(page.getByText('For full Unraid recovery, include')).toBeVisible();
      await settle(page);
      await expectNoHorizontalOverflow(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `/setup step 5 (Unraid warning) ${viewport.width}px ${theme}`);
      }
    }
  });

  test('not-found page', async ({ page }) => {
    await page.goto(demoUrl('/no-such-page'), { waitUntil: 'networkidle' });
    await settle(page);
    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `404 ${theme}`);
    }
  });
});

test.describe('axe: states behind a click', () => {
  test.beforeEach(async ({ page }) => {
    await isolateFromBackend(page);
    await page.setViewportSize(PHONE);
  });

  test('generated recovery kit (repository table) at 375px', async ({ page }) => {
    await page.goto(demoUrl('/settings/recovery-kit'), { waitUntil: 'networkidle' });
    await settle(page);
    await page.getByRole('button', { name: 'Generate recovery kit' }).click();
    await expect(page.getByRole('region', { name: 'Repositories' })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `recovery kit generated ${theme}`);
    }
  });

  test('directory suggestions after Scan, both widths and themes', async ({ page }) => {
    for (const viewport of [DESKTOP, PHONE]) {
      await page.setViewportSize(viewport);
      await page.goto(demoUrl('/settings/directories'), { waitUntil: 'networkidle' });
      await settle(page);
      await page.getByRole('button', { name: 'Scan', exact: true }).click();
      // One suggestion of each priority: critical, recommended, optional.
      for (const priority of ['critical', 'recommended', 'optional']) {
        await expect(page.getByText(priority, { exact: true }).first()).toBeVisible();
      }
      await settle(page);
      await expectNoHorizontalOverflow(page);
      for (const theme of ['dark', 'light'] as const) {
        await setTheme(page, theme);
        await expectNoAxeViolations(page, `directory suggestions ${viewport.width}px ${theme}`);
      }
    }
  });

  test('log entries render, wrap at 375px and can be scrolled from the keyboard', async ({ page }) => {
    await page.goto(demoUrl('/logs'), { waitUntil: 'networkidle' });
    await READY['/logs'](page);
    const entries = page.getByRole('region', { name: 'Log entries' });
    await expect(entries).toHaveAttribute('tabindex', '0');
    // The fixed columns no longer push the box into sideways scrolling.
    const { scrollWidth, clientWidth } = await entries.evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }));
    expect(scrollWidth, 'log box scrolls sideways at 375px').toBeLessThanOrEqual(clientWidth);
    await entries.focus();
    await expect(entries).toBeFocused();
  });

  test('expanded restore drill (results table) at 375px', async ({ page }) => {
    await page.goto(demoUrl('/'), { waitUntil: 'networkidle' });
    await settle(page);
    const firstDrill = page.locator('button[aria-controls^="drill-detail-"]').first();
    await firstDrill.click();
    await expect(firstDrill).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('region', { name: 'Drill results' })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `drill expanded ${theme}`);
    }
  });
});

test.describe('keyboard: app shell', () => {
  test.beforeEach(async ({ page }) => {
    await isolateFromBackend(page);
  });

  test('skip link is the first tab stop and moves focus to main', async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(demoUrl('/backups'), { waitUntil: 'networkidle' });
    await settle(page);
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: 'Skip to main content' });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
    await page.keyboard.press('Enter');
    await expect(page.locator('main')).toBeFocused();
  });

  test('sidebar marks the current page', async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(demoUrl('/snapshots'), { waitUntil: 'networkidle' });
    const nav = page.getByRole('navigation', { name: 'Main' });
    await expect(nav.getByRole('link', { name: 'Snapshots' })).toHaveAttribute('aria-current', 'page');
    await expect(nav.getByRole('link', { name: 'Backups' })).not.toHaveAttribute('aria-current', 'page');
  });

  test('closed mobile sidebar is out of the tab order; opening focuses it, Escape closes it', async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(demoUrl('/backups'), { waitUntil: 'networkidle' });
    await settle(page);

    const toggle = page.getByRole('button', { name: 'Toggle sidebar' });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    // Off-canvas links are inert: not focusable and not exposed.
    await expect(page.locator('#app-sidebar nav')).toHaveAttribute('inert', '');
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press('Tab');
      const inSidebar = await page.evaluate(() => !!document.activeElement?.closest('#app-sidebar'));
      expect(inSidebar, 'focus moved into the closed sidebar').toBe(false);
    }

    await toggle.focus();
    await page.keyboard.press('Enter');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#app-sidebar nav')).not.toHaveAttribute('inert', '');
    // Opening moves focus into the (earlier in DOM order) sidebar.
    const dashboardLink = page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Dashboard' });
    await expect(dashboardLink).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toBeFocused();
  });

  test('command palette: combobox semantics, one step per arrow key, Escape closes', async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(demoUrl('/'), { waitUntil: 'networkidle' });
    await settle(page);

    await page.keyboard.press('Control+k');
    const input = page.getByRole('combobox', { name: 'Search pages' });
    await expect(input).toBeFocused();
    await expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-0');
    await page.keyboard.press('ArrowDown');
    // A single press moves a single option (the key used to be handled twice).
    await expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-1');
    await expect(page.locator('#command-palette-option-1')).toHaveAttribute('aria-selected', 'true');
    await expectNoAxeViolations(page, 'command palette', '[aria-label="Command palette"]');

    await page.keyboard.press('Escape');
    await expect(input).toHaveCount(0);
  });
});

test.describe('keyboard: dialogs', () => {
  test.beforeEach(async ({ page }) => {
    await isolateFromBackend(page);
  });

  test('confirm-delete modal traps focus, closes on Escape and returns focus', async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(demoUrl('/settings/jobs'), { waitUntil: 'networkidle' });
    await settle(page);

    const trigger = page.getByRole('button', { name: /^Delete / }).first();
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: 'Delete Backup Job' });
    await expect(dialog).toBeVisible();
    // Focus starts in the confirmation field.
    await expect(dialog.getByRole('textbox')).toBeFocused();

    // Tab and Shift+Tab never leave the dialog.
    for (const key of ['Tab', 'Tab', 'Tab', 'Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab']) {
      await page.keyboard.press(key);
      const inside = await dialog.evaluate((el) => el.contains(document.activeElement));
      expect(inside, `focus left the dialog after ${key}`).toBe(true);
    }

    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `confirm modal ${theme}`, '[role="dialog"]');
    }

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('a drag from the confirmation field onto the backdrop keeps the dialog and its text', async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await page.goto(demoUrl('/settings/targets'), { waitUntil: 'networkidle' });
    await settle(page);

    await page.getByRole('button', { name: /^Delete / }).first().click();
    const dialog = page.getByRole('dialog');
    const field = dialog.getByRole('textbox');
    await expect(field).toBeFocused();
    await field.fill('DELET');

    const fieldBox = (await field.boundingBox())!;
    const panelBox = (await dialog.boundingBox())!;
    const y = fieldBox.y + fieldBox.height / 2;
    const outsideX = panelBox.x - 20;
    expect(outsideX, 'no backdrop to the left of the panel').toBeGreaterThan(0);

    // Select the typed text by dragging, releasing past the panel's left edge.
    // The browser sends that click to the backdrop layer.
    await page.mouse.move(fieldBox.x + fieldBox.width - 4, y);
    await page.mouse.down();
    await page.mouse.move(outsideX, y, { steps: 5 });
    await page.mouse.up();
    // A closing dialog stays in the DOM while it fades out, so "still visible"
    // proves nothing here. Closing moves focus back to the Delete button and
    // clears the field; neither may happen, and typing must still reach it.
    await expect(field).toBeFocused();
    await page.keyboard.press('End');
    await page.keyboard.type('E');
    await expect(field).toHaveValue('DELETE');
    await expect(dialog.getByRole('button', { name: 'Delete Target' })).toBeEnabled();

    // A press that starts and ends on the backdrop still closes it.
    await page.mouse.click(outsideX, y);
    await expect(dialog).toHaveCount(0);
  });

  test('restore dialog is labelled, passes axe and closes on Escape', async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(demoUrl('/snapshots'), { waitUntil: 'networkidle' });
    await settle(page);

    const trigger = page.getByRole('button', { name: /^Restore snapshot / }).first();
    await trigger.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expectNoHorizontalOverflow(page);
    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `restore dialog ${theme}`, '[role="dialog"]');
    }
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('snapshot browser dialog passes axe and closes on Escape', async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto(demoUrl('/snapshots'), { waitUntil: 'networkidle' });
    await settle(page);

    const trigger = page.getByRole('button', { name: /^Browse snapshot / }).first();
    await trigger.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expectNoHorizontalOverflow(page);
    for (const theme of ['dark', 'light'] as const) {
      await setTheme(page, theme);
      await expectNoAxeViolations(page, `snapshot browser ${theme}`, '[role="dialog"]');
    }
    // Each entry is one labelled checkbox; toggling it (keyboard or click)
    // updates the selection.
    const entry = dialog.getByRole('checkbox', { name: /appdata/ });
    await entry.focus();
    await page.keyboard.press('Space');
    await expect(entry).toBeChecked();
    await expect(dialog.getByText('1 selected')).toBeVisible();
    await entry.click();
    await expect(dialog.getByText('0 selected')).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
});
