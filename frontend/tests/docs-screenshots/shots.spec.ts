import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Locator, type Page } from '@playwright/test';

// Screenshots for docs/ and README.md, taken in demo mode (sample data from
// src/lib/api/mock.ts, never a real server). Run scripts/docs-screenshots.sh.
//
// The guide uses the dark theme (the app's default) throughout. The README
// shows the dashboard, Databases and Snapshots pages in both themes through
// <picture>, so those three are taken twice.

const OUT_DIR = resolve(
	process.env.DOCS_SCREENSHOTS_OUT_DIR ?? fileURLToPath(new URL('../../../docs/images', import.meta.url)),
);

// Mock data is relative to "now"; a fixed clock keeps "12m ago" stable.
const FIXED_NOW = new Date('2026-06-15T12:00:00Z');

type Theme = 'dark' | 'light';

type Shot = {
	file: string; // written to OUT_DIR
	path: string;
	theme?: Theme; // default dark
	height?: number; // viewport height, default 800
	// Fake timers that never advance: demo mode is always signed in, so
	// /setup would redirect to the dashboard once its session call resolved.
	frozen?: boolean;
	session?: Record<string, string>; // sessionStorage entries
	act?: (page: Page) => Promise<void>;
	// What to capture: the viewport (default), or an element plus a margin.
	crop?: (page: Page) => Locator;
	pad?: number;
};

const main = (page: Page) => page.locator('main').first();

const SHOTS: Shot[] = [
	// README hero and the guide's home page.
	...(['dark', 'light'] as const).map((theme) => ({ file: `dashboard-${theme}.png`, path: '/', theme })),
	...(['dark', 'light'] as const).map((theme) => ({ file: `databases-${theme}.png`, path: '/databases', theme, height: 900 })),
	...(['dark', 'light'] as const).map((theme) => ({ file: `snapshots-${theme}.png`, path: '/snapshots', theme })),
	{
		file: 'setup-checklist.png',
		path: '/',
		crop: (page) => page.getByTestId('setup-checklist'),
	},
	{
		file: 'setup-wizard-destination.png',
		path: '/setup',
		frozen: true,
		session: { arkive_setup_step: '3' },
		act: async (page) => {
			await page.getByRole('button', { name: /Backblaze B2/ }).click();
			await expect(page.getByText('Key ID')).toBeVisible();
		},
		crop: (page) => page.locator('main > div').first(),
	},
	{
		file: 'restore-drill-failed.png',
		path: '/',
		act: async (page) => {
			const panel = page.getByTestId('restore-drills-panel');
			await panel.locator('button[aria-expanded]').nth(1).click();
			await expect(panel.locator('table')).toBeVisible();
		},
		crop: (page) => page.getByTestId('restore-drills-panel'),
	},
	{
		file: 'guided-restore-review.png',
		path: '/restore/database',
		act: async (page) => {
			await page.getByRole('radio', { name: /immich/ }).click();
			await page.getByRole('button', { name: 'Check snapshot' }).click();
			await expect(page.getByTestId('guided-restore-review')).toBeVisible();
		},
		crop: (page) => page.getByTestId('guided-restore-review'),
	},
	{ file: 'notifications-add-channel.png', path: '/settings/notifications', act: (page) => page.getByRole('button', { name: 'Add Channel' }).click(), crop: main, pad: 0 },
	{ file: 'monitoring-settings.png', path: '/settings/monitoring', crop: main, pad: 0 },
	{ file: 'storage-targets.png', path: '/settings/targets', crop: main, pad: 0 },
	{
		file: 'recovery-kit.png',
		path: '/settings/recovery-kit',
		act: async (page) => {
			await page.getByRole('button', { name: 'Generate recovery kit' }).click();
			await expect(page.getByTestId('recovery-kit-password')).toBeVisible();
		},
		crop: main,
		pad: 0,
	},
];

async function open(page: Page, shot: Shot) {
	await page.setViewportSize({ width: 1280, height: shot.height ?? 800 });
	if (shot.frozen) {
		await page.clock.install({ time: new Date(FIXED_NOW.getTime() - 1000) });
		await page.clock.pauseAt(FIXED_NOW);
	} else {
		await page.clock.setFixedTime(FIXED_NOW);
	}
	await page.addInitScript(
		([theme, entries]) => {
			try {
				localStorage.setItem('arkive_theme', theme);
				for (const [key, value] of Object.entries(entries)) sessionStorage.setItem(key, value);
			} catch {
				// storage unavailable: the app falls back to dark
			}
		},
		[shot.theme ?? 'dark', shot.session ?? {}] as const,
	);
	await page.goto(`${shot.path}?demo=true`);
	await page.waitForLoadState('networkidle');
	await page.evaluate(() => document.fonts.ready);
	await page.waitForTimeout(500);
}

test.beforeAll(() => mkdirSync(OUT_DIR, { recursive: true }));

for (const shot of SHOTS) {
	test(shot.file, async ({ page }) => {
		await open(page, shot);
		if (shot.act) {
			await shot.act(page);
			await page.waitForTimeout(400);
		}
		const options = { path: resolve(OUT_DIR, shot.file), animations: 'disabled', caret: 'hide', scale: 'css' } as const;
		if (!shot.crop) {
			await page.screenshot(options);
			return;
		}
		const viewportBox = await shot.crop(page).boundingBox();
		if (!viewportBox) throw new Error(`${shot.file}: nothing to crop to`);
		// boundingBox() is relative to the viewport, which a click may have
		// scrolled; a full-page clip is in page coordinates.
		const [scrollX, scrollY, width] = await page.evaluate(() => [
			window.scrollX,
			window.scrollY,
			document.documentElement.scrollWidth,
		]);
		const box = { ...viewportBox, x: viewportBox.x + scrollX, y: viewportBox.y + scrollY };
		const pad = shot.pad ?? 16;
		const x = Math.max(0, Math.floor(box.x - pad));
		const y = Math.max(0, Math.floor(box.y - pad));
		await page.screenshot({
			...options,
			fullPage: true,
			clip: {
				x,
				y,
				width: Math.min(width, Math.ceil(box.x + box.width + pad)) - x,
				height: Math.ceil(box.y + box.height + pad) - y,
			},
		});
	});
}
