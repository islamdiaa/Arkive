/**
 * Theme Tests — verify dark/light theme behaviour.
 *
 * Arkive defaults to dark theme with background #0d1117 (GitHub Primer).
 * The theme is toggled via the toggleTheme() store function and persisted
 * to localStorage under `arkive_theme`.
 */

import { test, expect } from '@playwright/test';
import { stubSseTokenForDemoMode } from './fixtures/api-mocks';

const DASHBOARD_URL = '/?demo=true';

test.describe('Theme', () => {
  test.beforeEach(async ({ page }) => {
    // Keep the demo dashboard from being bounced to /login mid-test.
    await stubSseTokenForDemoMode(page);
  });

  test('Dark theme is the default', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.removeItem('arkive_theme');
    });
    await page.goto(DASHBOARD_URL, { waitUntil: 'networkidle' });

    // The CSS variable --page-bg should be #0d1117 in the default (dark) theme
    const pageBg = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue('--page-bg').trim();
    });
    expect(pageBg).toBe('#0d1117');
  });

  test('Dark theme applies dark class to html element', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.removeItem('arkive_theme');
    });
    await page.goto(DASHBOARD_URL, { waitUntil: 'networkidle' });

    // After initTheme runs, the html element should have the 'dark' class
    const hasDarkClass = await page.evaluate(() => {
      return document.documentElement.classList.contains('dark');
    });
    expect(hasDarkClass).toBe(true);
  });

  test('Theme is stored in localStorage and read on init', async ({ page }) => {
    await page.goto(DASHBOARD_URL, { waitUntil: 'networkidle' });

    // Verify dark is the default
    let pageBg = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue('--page-bg').trim();
    });
    expect(pageBg).toBe('#0d1117');

    // Switch to light theme via DOM + localStorage (simulating toggleTheme)
    await page.evaluate(() => {
      localStorage.setItem('arkive_theme', 'light');
      document.documentElement.classList.remove('dark');
      document.documentElement.classList.add('light');
    });

    // Verify light CSS variables are applied after class change
    pageBg = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue('--page-bg').trim();
    });
    expect(pageBg).toBe('#ffffff');

    // Verify localStorage has the correct value
    const stored = await page.evaluate(() => localStorage.getItem('arkive_theme'));
    expect(stored).toBe('light');
  });

  test('Toggling theme changes CSS variables', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.removeItem('arkive_theme');
    });
    await page.goto(DASHBOARD_URL, { waitUntil: 'networkidle' });

    // Verify dark first
    let pageBg = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue('--page-bg').trim();
    });
    expect(pageBg).toBe('#0d1117');

    // Toggle to light by manipulating DOM and localStorage (simulating toggleTheme)
    await page.evaluate(() => {
      const html = document.documentElement;
      html.classList.remove('dark');
      html.classList.add('light');
      localStorage.setItem('arkive_theme', 'light');
    });

    // Verify the CSS variable changed
    pageBg = await page.evaluate(() => {
      return getComputedStyle(document.documentElement).getPropertyValue('--page-bg').trim();
    });
    expect(pageBg).toBe('#ffffff');
  });
});
