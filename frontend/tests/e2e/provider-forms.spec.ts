/**
 * Provider Forms E2E Tests
 *
 * Tests every cloud storage provider form in the setup wizard step 3.
 * Verifies field visibility, required-field validation, and cross-provider switching.
 *
 * Uses page.route() mocks identical to setup-wizard.spec.ts so the app
 * thinks setup is NOT complete and stays on /setup.
 */

import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Shared API mocks (same pattern as setup-wizard.spec.ts)
// ---------------------------------------------------------------------------

async function mockSetupAPIs(page: Page) {
  await page.route('**/api/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: '3.0.0', status: 'ok', setup_completed: false }),
    })
  );
  await page.route('**/api/auth/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ setup_required: true }),
    })
  );
  await page.route('**/api/events/**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
  );
  await page.route('**/api/discover/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ containers: [], items: [], total: 0 }),
    })
  );
  await page.route('**/api/directories/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ directories: [], items: [], total: 0, suggestions: [] }),
    })
  );

  await page.addInitScript(() => {
    localStorage.removeItem('arkive_api_key');
    localStorage.removeItem('arkive_theme');
    sessionStorage.removeItem('arkive_setup_step');
  });
}

// ---------------------------------------------------------------------------
// Navigate from step 1 through step 2 to land on step 3 (Backup Destination)
// ---------------------------------------------------------------------------

async function navigateToStep3(page: Page) {
  await page.goto('/setup');

  // Step 1: fill matching 12+ char passwords
  const passwordInputs = page.locator('input[type="password"]');
  await passwordInputs.nth(0).fill('securepassword123');
  await passwordInputs.nth(1).fill('securepassword123');
  await page.locator('button:has-text("Next")').click();

  // Step 2: Container Discovery — scan then proceed
  await expect(page.locator('h2')).toContainText('Container Discovery');
  await page.locator('button:has-text("Scan for Containers")').click();
  await page.locator('button:has-text("Next")').click();

  // Now on step 3
  await expect(page.locator('h2')).toContainText('Backup Destination');
}

// Convenience: the Next button on step 3
function nextButton(page: Page) {
  return page.locator('button:has-text("Next")');
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Provider Forms — Setup Wizard Step 3', () => {
  test.beforeEach(async ({ page }) => {
    await mockSetupAPIs(page);
  });

  // -----------------------------------------------------------------------
  // Backblaze B2
  // -----------------------------------------------------------------------

  test('B2 shows credential fields when selected', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Backblaze B2")').click();

    await expect(page.locator('#b2-key-id')).toBeVisible();
    await expect(page.locator('#b2-app-key')).toBeVisible();
    await expect(page.locator('#b2-bucket')).toBeVisible();
  });

  test('B2 Next disabled without credentials', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Backblaze B2")').click();

    // All fields empty — Next must be disabled
    await expect(nextButton(page)).toBeDisabled();
  });

  test('B2 Next enabled with all fields filled', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Backblaze B2")').click();

    await page.locator('#b2-key-id').fill('00123456789abcdef0000000000');
    await page.locator('#b2-app-key').fill('K001someAppKeyValue');
    await page.locator('#b2-bucket').fill('my-arkive-backups');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Amazon S3
  // -----------------------------------------------------------------------

  test('S3 shows credential fields', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Amazon S3")').click();

    await expect(page.locator('#s3-endpoint')).toBeVisible();
    await expect(page.locator('#s3-access-key')).toBeVisible();
    await expect(page.locator('#s3-secret-key')).toBeVisible();
    await expect(page.locator('#s3-bucket')).toBeVisible();
    await expect(page.locator('#s3-region')).toBeVisible();
  });

  test('S3 Next disabled without credentials', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Amazon S3")').click();

    // Required: access key, secret key, bucket (endpoint and region optional)
    await expect(nextButton(page)).toBeDisabled();
  });

  test('S3 Next enabled with required fields', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Amazon S3")').click();

    await page.locator('#s3-access-key').fill('AKIAIOSFODNN7EXAMPLE');
    await page.locator('#s3-secret-key').fill('wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY');
    await page.locator('#s3-bucket').fill('my-backup-bucket');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Wasabi
  // -----------------------------------------------------------------------

  test('Wasabi shows credential fields', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Wasabi")').click();

    await expect(page.locator('#wasabi-access-key')).toBeVisible();
    await expect(page.locator('#wasabi-secret-key')).toBeVisible();
    await expect(page.locator('#wasabi-bucket')).toBeVisible();
    await expect(page.locator('#wasabi-region')).toBeVisible();
  });

  test('Wasabi Next enabled with credentials', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Wasabi")').click();

    await page.locator('#wasabi-access-key').fill('WASABIACCESSKEY');
    await page.locator('#wasabi-secret-key').fill('WASABISECRETKEY');
    await page.locator('#wasabi-bucket').fill('wasabi-backups');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // SFTP
  // -----------------------------------------------------------------------

  test('SFTP shows connection fields', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("SFTP Server")').click();

    await expect(page.locator('#sftp-host')).toBeVisible();
    await expect(page.locator('#sftp-port')).toBeVisible();
    await expect(page.locator('#sftp-username')).toBeVisible();
    await expect(page.locator('#sftp-password')).toBeVisible();
    await expect(page.locator('#sftp-path')).toBeVisible();
  });

  test('SFTP Next disabled without host', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("SFTP Server")').click();

    // Validation requires host AND username — both empty here
    await expect(nextButton(page)).toBeDisabled();
  });

  test('SFTP Next enabled with host and username', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("SFTP Server")').click();

    await page.locator('#sftp-host').fill('192.168.1.100');
    await page.locator('#sftp-username').fill('arkive');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Dropbox
  // -----------------------------------------------------------------------

  test('Dropbox shows token field', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Dropbox")').click();

    await expect(page.locator('#dropbox-token')).toBeVisible();
    // Also verify the helper text about Dropbox App Console
    await expect(page.getByText('Dropbox App Console')).toBeVisible();
  });

  test('Dropbox Next disabled without token', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Dropbox")').click();

    await expect(nextButton(page)).toBeDisabled();
  });

  test('Dropbox Next enabled with token', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Dropbox")').click();

    await page.locator('#dropbox-token').fill('sl.BnXQ_dropbox_oauth_token_example');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Google Drive
  // -----------------------------------------------------------------------

  test('Google Drive shows token field', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Google Drive")').click();

    await expect(page.locator('#gdrive-token')).toBeVisible();
    // Helper text about rclone authorize
    await expect(page.getByText('rclone authorize')).toBeVisible();
  });

  // Setup mirrors the backend's provider validation (api/targets.py): a
  // Google Drive target needs a client ID as well as the OAuth token.
  test('Google Drive Next disabled with token but no client ID', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Google Drive")').click();

    await page.locator('#gdrive-token').fill('{"access_token":"ya29.example","token_type":"Bearer"}');

    await expect(nextButton(page)).toBeDisabled();
  });

  test('Google Drive Next enabled with client ID and token', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Google Drive")').click();

    await page.locator('#gdrive-client-id').fill('123456789.apps.googleusercontent.com');
    await page.locator('#gdrive-token').fill('{"access_token":"ya29.example","token_type":"Bearer"}');

    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Local Path
  // -----------------------------------------------------------------------

  test('Local shows path field with default', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Local Path")').click();

    const pathInput = page.locator('#local-path');
    await expect(pathInput).toBeVisible();
    await expect(pathInput).toHaveValue('/mnt/user/backups');
  });

  test('Local Next enabled by default', async ({ page }) => {
    await navigateToStep3(page);
    await page.locator('button:has-text("Local Path")').click();

    // Default path "/mnt/user/backups" is pre-filled, so validation passes
    await expect(nextButton(page)).toBeEnabled();
  });

  // -----------------------------------------------------------------------
  // Cross-provider behavior
  // -----------------------------------------------------------------------

  test('Switching provider clears previous form', async ({ page }) => {
    await navigateToStep3(page);

    // Select B2 and fill in fields
    await page.locator('button:has-text("Backblaze B2")').click();
    await page.locator('#b2-key-id').fill('some-key-id');
    await page.locator('#b2-app-key').fill('some-app-key');
    await page.locator('#b2-bucket').fill('some-bucket');

    // Switch to Amazon S3
    await page.locator('button:has-text("Amazon S3")').click();

    // B2 fields should be gone, S3 fields should be visible
    await expect(page.locator('#b2-key-id')).not.toBeVisible();
    await expect(page.locator('#b2-app-key')).not.toBeVisible();
    await expect(page.locator('#b2-bucket')).not.toBeVisible();

    await expect(page.locator('#s3-access-key')).toBeVisible();
    await expect(page.locator('#s3-secret-key')).toBeVisible();
    await expect(page.locator('#s3-bucket')).toBeVisible();
  });

  test('Provider selection highlights selected button', async ({ page }) => {
    await navigateToStep3(page);

    // Select Backblaze B2
    const b2Button = page.locator('button:has-text("Backblaze B2")');
    await b2Button.click();

    // The selected provider button should have border-primary in its class
    await expect(b2Button).toHaveClass(/border-primary/);

    // A non-selected provider should NOT have border-primary
    const s3Button = page.locator('button:has-text("Amazon S3")');
    await expect(s3Button).not.toHaveClass(/border-primary/);

    // Now switch to S3
    await s3Button.click();
    await expect(s3Button).toHaveClass(/border-primary/);
    await expect(b2Button).not.toHaveClass(/border-primary/);
  });
});
