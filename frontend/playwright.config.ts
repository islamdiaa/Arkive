import { defineConfig } from '@playwright/test';

/**
 * Playwright config for Arkive E2E tests.
 *
 * Two servers are launched:
 *   1. FastAPI backend on port 8200 (serves API at /api/*)
 *   2. SvelteKit dev server on port 5173 (proxies /api to backend)
 *
 * Tests that exercise the browser UI hit the dev server on 5173.
 * Tests that exercise the raw API use request contexts targeting 8200.
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  // The dev server compiles each route on first request; on a cold or busy
  // machine (CI runners, the first spec) that can exceed the 5s default.
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 2 : 0,
  // Tests share one backend process and mutable auth/rate-limit state.
  // Keep one worker to avoid cross-project lockout races.
  workers: 1,
  fullyParallel: false,
  // Outputs live under .playwright/ (gitignored). CI uploads the HTML report
  // from .playwright/report on failure.
  outputDir: '.playwright/test-results',
  reporter: [['list'], ['html', { open: 'never', outputFolder: '.playwright/report' }]],

  use: {
    baseURL: 'http://127.0.0.1:5173',
    headless: true,
    viewport: { width: 1280, height: 720 },
    actionTimeout: 10_000,
    trace: 'retain-on-failure',
  },

  projects: [
    {
      name: 'setup-wizard',
      testMatch: 'setup-wizard.spec.ts',
    },
    {
      name: 'api-smoke',
      testMatch: 'api-smoke.spec.ts',
      use: { baseURL: 'http://127.0.0.1:8200' },
    },
    {
      name: 'navigation',
      testMatch: 'navigation.spec.ts',
    },
    {
      name: 'theme',
      testMatch: 'theme.spec.ts',
    },
    {
      name: 'targets',
      testMatch: 'targets.spec.ts',
    },
    {
      name: 'auth',
      testMatch: 'auth.spec.ts',
    },
    {
      name: 'dashboard',
      testMatch: 'dashboard.spec.ts',
    },
    {
      name: 'backup-flow',
      testMatch: 'backup-flow.spec.ts',
    },
    {
      name: 'snapshots',
      testMatch: 'snapshots.spec.ts',
    },
    {
      name: 'settings-crud',
      testMatch: 'settings-crud.spec.ts',
    },
    {
      name: 'restore',
      testMatch: 'restore.spec.ts',
    },
    {
      name: 'activity-logs',
      testMatch: 'activity-logs.spec.ts',
    },
    {
      name: 'databases',
      testMatch: 'databases.spec.ts',
    },
    {
      name: 'error-states',
      testMatch: 'error-states.spec.ts',
    },
    {
      name: 'sse-connection',
      testMatch: 'sse-connection.spec.ts',
    },
    {
      name: 'provider-forms',
      testMatch: /provider-forms\.spec\.ts$/,
    },
    {
      name: 'backup-run-detail',
      testMatch: 'backup-run-detail.spec.ts',
    },
    {
      name: 'sidebar-security',
      testMatch: 'sidebar-security.spec.ts',
    },
  ],

  webServer: [
    {
      command: './tests/e2e/start-backend.sh',
      port: 8200,
      reuseExistingServer: !process.env.CI,
      timeout: 45_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: './tests/e2e/start-frontend.sh',
      port: 5173,
      reuseExistingServer: !process.env.CI,
      timeout: 45_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
});
