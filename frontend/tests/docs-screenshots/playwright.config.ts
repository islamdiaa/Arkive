import { resolve } from 'node:path';
import { defineConfig } from '@playwright/test';

/**
 * Screenshots for the user guide and the README (see scripts/docs-screenshots.sh).
 *
 * Renders pages in demo mode (?demo=true, no backend) from the static build in
 * $DOCS_SCREENSHOTS_BUILD_DIR (default: build), served on port 5321, and
 * writes PNGs into $DOCS_SCREENSHOTS_OUT_DIR (default: docs/images). Fixed
 * clock, viewport, locale and time zone, so a rerun on the same build in the
 * same Playwright image gives the same pixels.
 */
const buildDir = resolve(process.env.DOCS_SCREENSHOTS_BUILD_DIR ?? 'build');

export default defineConfig({
	testDir: '.',
	testMatch: 'shots.spec.ts',
	timeout: 60_000,
	retries: 0,
	workers: process.env.DOCS_SCREENSHOTS_WORKERS ? Number(process.env.DOCS_SCREENSHOTS_WORKERS) : 2,
	outputDir: '../../.playwright/docs-screenshots/results',
	reporter: [['list']],
	use: {
		baseURL: 'http://127.0.0.1:5321',
		headless: true,
		viewport: { width: 1280, height: 800 },
		deviceScaleFactor: 1,
		locale: 'en-US',
		timezoneId: 'UTC',
	},
	webServer: {
		command: `node ../visual/serve.mjs "${buildDir}" 5321`,
		url: 'http://127.0.0.1:5321/',
		reuseExistingServer: false,
		timeout: 15_000,
	},
});
