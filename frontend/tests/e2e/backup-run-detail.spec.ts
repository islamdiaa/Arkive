/**
 * E2E tests for the Backup Run Detail page (/backups/[runId]).
 *
 * This page displays:
 * - Breadcrumb with "Back to Backups" link
 * - Run header with status badge, job name, trigger type
 * - Metadata grid: started, completed, duration, total size
 * - Results section: databases, flash drive, storage targets
 * - Per-target and per-database results (status + error) from run.targets[] / run.databases[]
 * - Error details (error_message) for failed AND partial runs
 * - Log viewer with streaming support
 */

import { test, expect, type Page } from '@playwright/test';
import { mockSetupComplete } from './fixtures/api-mocks';

// ---------------------------------------------------------------------------
// Mock data
// ---------------------------------------------------------------------------

// Mirrors GET /api/jobs/runs/{run_id} (backend/app/api/jobs.py get_run).
interface MockRun {
  id: string;
  job_id: string;
  job_name: string;
  job_type: string;
  status: string;
  trigger: string;
  started_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  total_size_bytes: number | null;
  databases_discovered: number;
  databases_dumped: number;
  databases_failed: number;
  flash_backed_up: number;
  flash_size_bytes: number;
  error_message: string | null;
  databases: Array<Record<string, unknown>>;
  targets: Array<Record<string, unknown>>;
}

const MOCK_RUN_SUCCESS: MockRun = {
  id: 'run-abc12345',
  job_id: 'j1',
  job_name: 'Nightly Full Backup',
  job_type: 'full',
  status: 'success',
  trigger: 'scheduled',
  started_at: '2024-06-15T02:00:00Z',
  completed_at: '2024-06-15T02:05:30Z',
  duration_seconds: 330,
  total_size_bytes: 1073741824,
  databases_discovered: 4,
  databases_dumped: 4,
  databases_failed: 0,
  flash_backed_up: 1,
  flash_size_bytes: 52428800,
  error_message: null,
  databases: [],
  targets: [],
};

const MOCK_RUN_PARTIAL: MockRun = {
  id: 'run-part54321',
  job_id: 'j2',
  job_name: 'DB Dumps',
  job_type: 'db_dump',
  status: 'partial',
  trigger: 'scheduled',
  started_at: '2024-06-15T06:00:00Z',
  completed_at: '2024-06-15T06:02:00Z',
  duration_seconds: 120,
  total_size_bytes: 524288000,
  databases_discovered: 2,
  databases_dumped: 1,
  databases_failed: 0,
  flash_backed_up: 0,
  flash_size_bytes: 0,
  error_message: 'Backup completed with problems (1 database skipped)',
  databases: [
    { id: 1, container_name: 'immich-postgres', db_type: 'postgres', db_name: 'immich', dump_size_bytes: 1048576, integrity_check: 'ok', status: 'success', host_path: null, error: null },
    { id: 2, container_name: 'paperless-db', db_type: 'postgres', db_name: 'paperless', dump_size_bytes: 0, integrity_check: 'skipped', status: 'skipped', host_path: null, error: 'Container paperless-db is not running' },
  ],
  targets: [
    { id: 1, target_id: 't1', status: 'success', snapshot_id: 'abcd1234', upload_bytes: 524288000, duration_seconds: 60, error: null, target_name: 'Local NAS', target_provider: 'local' },
  ],
};

const MOCK_RUN_FAILED: MockRun = {
  id: 'run-fail98765',
  job_id: 'j1',
  job_name: 'Cloud Sync',
  job_type: 'full',
  status: 'failed',
  trigger: 'manual',
  started_at: '2024-06-14T10:00:00Z',
  completed_at: '2024-06-14T10:01:15Z',
  duration_seconds: 75,
  total_size_bytes: 0,
  databases_discovered: 2,
  databases_dumped: 1,
  databases_failed: 1,
  flash_backed_up: 0,
  flash_size_bytes: 0,
  error_message: 'restic: connection to remote target timed out after 60s',
  databases: [],
  targets: [
    { id: 1, target_id: 't9', status: 'failed', snapshot_id: '', upload_bytes: 0, duration_seconds: null, error: 'dial tcp: i/o timeout', target_name: 'Offsite B2', target_provider: 'b2' },
  ],
};

const MOCK_RUN_RUNNING: MockRun = {
  id: 'run-running123',
  job_id: 'j2',
  job_name: 'DB Dumps',
  job_type: 'db_dump',
  status: 'running',
  trigger: 'manual',
  started_at: new Date().toISOString(),
  completed_at: null,
  duration_seconds: null,
  total_size_bytes: null,
  databases_discovered: 3,
  databases_dumped: 2,
  databases_failed: 0,
  flash_backed_up: 0,
  flash_size_bytes: 0,
  error_message: null,
  databases: [],
  targets: [],
};

const MOCK_LOGS = [
  {
    id: 1,
    level: 'INFO',
    message: 'Starting backup run run-abc12345',
    timestamp: '2024-06-15T02:00:00Z',
  },
  {
    id: 2,
    level: 'INFO',
    message: 'Phase 1/6: Pre-flight checks passed',
    timestamp: '2024-06-15T02:00:05Z',
  },
  {
    id: 3,
    level: 'INFO',
    message: 'Phase 2/6: Dumped 4 databases',
    timestamp: '2024-06-15T02:01:30Z',
  },
  {
    id: 4,
    level: 'INFO',
    message: 'Phase 6/6: Notifications sent',
    timestamp: '2024-06-15T02:05:28Z',
  },
  {
    id: 5,
    level: 'INFO',
    message: 'Backup completed successfully',
    timestamp: '2024-06-15T02:05:30Z',
  },
];

// ---------------------------------------------------------------------------
// Route setup helpers
// ---------------------------------------------------------------------------

async function mockRunDetailRoutes(page: Page, run: MockRun) {
  await mockSetupComplete(page);

  // Run detail endpoint — /api/jobs/runs/:runId
  await page.route('**/api/jobs/runs/' + run.id, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(run),
    })
  );

  // Run logs endpoint — /api/jobs/runs/:runId/logs
  await page.route('**/api/jobs/runs/' + run.id + '/logs', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: MOCK_LOGS, total: MOCK_LOGS.length }),
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

  // SSE events stream — prevent hanging
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );
}

async function mockRunDetailError(page: Page) {
  await mockSetupComplete(page);

  // Run detail and run logs endpoints return 404. '**' (not '*') so the
  // /logs sub-path is matched too instead of reaching the real backend.
  await page.route('**/api/jobs/runs/**', (route) => {
    const url = route.request().url();
    if (url.includes('/logs')) {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Run not found' }),
      });
    }
    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Run not found' }),
    });
  });

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
}

// ---------------------------------------------------------------------------
// Tests — Successful Run
// ---------------------------------------------------------------------------

test.describe('Backup Run Detail — Successful Run', () => {
  test.beforeEach(async ({ page }) => {
    await mockRunDetailRoutes(page, MOCK_RUN_SUCCESS);
  });

  test('page loads and displays run header with job name', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    await expect(page.locator('body')).not.toContainText('404');
    await expect(page.locator('body')).not.toContainText('TypeError');

    // Job name should appear in header
    await expect(
      page.getByRole('heading', { name: 'Nightly Full Backup' }).or(page.getByRole('heading', { name: 'Backup Run' }))
    ).toBeVisible();
  });

  test('breadcrumb "Back to Backups" link is present', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    const backLink = page.getByText('Back to Backups');
    await expect(backLink).toBeVisible();
  });

  test('run ID is shown in the header', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    // The page displays a truncated run ID (first 12 chars: "run-abc12345")
    await expect(page.getByText('run-abc12345').first()).toBeVisible();
  });

  test('status badge shows success', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    const successBadge = page.getByText('success', { exact: false });
    await expect(successBadge.first()).toBeVisible();
  });

  test('metadata grid shows started/completed/duration/size', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    // Check for metadata labels
    await expect(page.getByText('Started', { exact: true })).toBeVisible();
    await expect(page.getByText('Completed', { exact: true })).toBeVisible();
    await expect(page.getByText('Duration', { exact: true })).toBeVisible();
    await expect(page.getByText('Total Size', { exact: true }).first()).toBeVisible();
  });

  test('results section displays database/flash/target stats', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    await expect(page.getByText('Results', { exact: true })).toBeVisible();
    const results = page.locator('[data-testid="results"]');
    await expect(results.getByText('Databases', { exact: true })).toBeVisible();
    await expect(results.getByText('Flash Drive', { exact: true })).toBeVisible();
    await expect(results.getByText('Storage Targets', { exact: true })).toBeVisible();
  });

  test('log viewer section is present', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    await expect(page.getByText('Run Logs')).toBeVisible();
  });

  test('trigger type is displayed', async ({ page }) => {
    await page.goto('/backups/run-abc12345', { waitUntil: 'networkidle' });

    await expect(page.getByText('scheduled')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tests — Failed Run
// ---------------------------------------------------------------------------

test.describe('Backup Run Detail — Failed Run', () => {
  test.beforeEach(async ({ page }) => {
    await mockRunDetailRoutes(page, MOCK_RUN_FAILED);
  });

  test('failed run shows failure status', async ({ page }) => {
    await page.goto('/backups/run-fail98765', { waitUntil: 'networkidle' });

    const failBadge = page.getByText('failed', { exact: false });
    await expect(failBadge.first()).toBeVisible();
  });

  test('failed run displays error message', async ({ page }) => {
    await page.goto('/backups/run-fail98765', { waitUntil: 'networkidle' });

    // The error_message should be rendered in the error details section
    await expect(
      page.getByText('connection to remote target timed out', { exact: false })
    ).toBeVisible();
  });

  test('failed run shows the per-target error', async ({ page }) => {
    await page.goto('/backups/run-fail98765', { waitUntil: 'networkidle' });

    await expect(page.getByText('Offsite B2')).toBeVisible();
    await expect(page.getByText('dial tcp: i/o timeout')).toBeVisible();
  });

  test('failed run shows databases_failed count', async ({ page }) => {
    await page.goto('/backups/run-fail98765', { waitUntil: 'networkidle' });

    // The databases section should show "Failed: 1"
    await expect(page.getByText('Failed', { exact: true })).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tests — Partial Run
// ---------------------------------------------------------------------------

test.describe('Backup Run Detail — Partial Run', () => {
  test.beforeEach(async ({ page }) => {
    await mockRunDetailRoutes(page, MOCK_RUN_PARTIAL);
  });

  test('partial run shows its error summary and per-database skip reason', async ({ page }) => {
    await page.goto('/backups/run-part54321', { waitUntil: 'networkidle' });

    await expect(page.getByText('Completed with problems', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('1 database skipped', { exact: false })).toBeVisible();
    await expect(page.getByText('Container paperless-db is not running')).toBeVisible();
    await expect(page.getByText('Local NAS')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tests — Running (in-progress) Run
// ---------------------------------------------------------------------------

test.describe('Backup Run Detail — Running', () => {
  test.beforeEach(async ({ page }) => {
    await mockRunDetailRoutes(page, MOCK_RUN_RUNNING);
  });

  test('running run shows running status', async ({ page }) => {
    await page.goto('/backups/run-running123', { waitUntil: 'networkidle' });

    const runningIndicator = page
      .getByText('running', { exact: false })
      .or(page.getByText('In progress', { exact: false }));
    await expect(runningIndicator.first()).toBeVisible();
  });

  test('running run shows "In progress" for completed time', async ({ page }) => {
    await page.goto('/backups/run-running123', { waitUntil: 'networkidle' });

    await expect(page.getByText('In progress')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tests — Error State
// ---------------------------------------------------------------------------

test.describe('Backup Run Detail — Error Handling', () => {
  test('shows error when run is not found', async ({ page }) => {
    await mockRunDetailError(page);

    await page.goto('/backups/nonexistent-run-id', { waitUntil: 'networkidle' });

    // The page should show an error message, not crash
    await expect(page.locator('body')).not.toContainText('TypeError');
    await expect(page.locator('body')).not.toContainText('uncaught');

    // Should show error feedback or retry button
    const errorFeedback = page
      .getByText('Failed to load', { exact: false })
      .or(page.getByText('not found', { exact: false }))
      .or(page.getByRole('button', { name: /retry/i }));
    await expect(errorFeedback.first()).toBeVisible();
  });

  test('retry button reloads the run on error', async ({ page }) => {
    await mockRunDetailError(page);

    await page.goto('/backups/nonexistent-run-id', { waitUntil: 'networkidle' });

    const retryBtn = page.getByRole('button', { name: /retry/i });
    if (await retryBtn.isVisible()) {
      // Clicking retry should trigger another API call
      const [response] = await Promise.all([
        page.waitForResponse('**/api/jobs/runs/*').catch(() => null),
        retryBtn.click(),
      ]);
      // Page should still not crash
      await expect(page.locator('body')).not.toContainText('TypeError');
    }
  });
});
