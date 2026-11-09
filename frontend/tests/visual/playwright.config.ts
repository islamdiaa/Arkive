import { resolve } from 'node:path';
import { defineConfig } from '@playwright/test';

/**
 * Pixel comparison of two frontend builds (see scripts/visual-diff.sh).
 *
 * Every route is rendered in demo mode (?demo=true, no backend) from the
 * static build in $VISUAL_BUILD_DIR, served on port 5320, in both themes at
 * desktop and phone widths. Run once with --update-snapshots=all against the
 * baseline build, then without it against the candidate build: any pixel that
 * differs fails, with a diff image in the HTML report.
 */
const buildDir = resolve(process.env.VISUAL_BUILD_DIR ?? 'build');
const desktop = { width: 1280, height: 800 };
const phone = { width: 390, height: 844 };

export default defineConfig({
	testDir: '.',
	testMatch: 'routes.spec.ts',
	timeout: 60_000,
	retries: 0,
	workers: process.env.VISUAL_WORKERS ? Number(process.env.VISUAL_WORKERS) : 2,
	outputDir: '../../.playwright/visual/results',
	snapshotPathTemplate: '{testDir}/../../.playwright/visual/baseline/{projectName}/{arg}{ext}',
	reporter: [['list'], ['html', { open: 'never', outputFolder: '../../.playwright/visual/report' }]],
	expect: {
		// Full-page captures must match twice in a row; slow on a busy machine.
		timeout: 30_000,
		// threshold 0: any colour change counts. VISUAL_THRESHOLD=0.01 tolerates
		// anti-aliasing differences of a level or two per channel.
		toHaveScreenshot: {
			threshold: Number(process.env.VISUAL_THRESHOLD ?? 0),
			maxDiffPixels: 0,
			animations: 'disabled',
			caret: 'hide',
			scale: 'css',
		},
	},
	use: {
		baseURL: 'http://127.0.0.1:5320',
		headless: true,
		deviceScaleFactor: 1,
		locale: 'en-US',
		timezoneId: 'UTC',
	},
	projects: [
		{ name: 'desktop-dark', use: { viewport: desktop }, metadata: { theme: 'dark' } },
		{ name: 'desktop-light', use: { viewport: desktop }, metadata: { theme: 'light' } },
		{ name: 'phone-dark', use: { viewport: phone }, metadata: { theme: 'dark' } },
		{ name: 'phone-light', use: { viewport: phone }, metadata: { theme: 'light' } },
	],
	webServer: {
		command: `node serve.mjs "${buildDir}" 5320`,
		url: 'http://127.0.0.1:5320/',
		reuseExistingServer: false,
		timeout: 15_000,
	},
});
