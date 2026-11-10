import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';

// Mock data is relative to "now"; a fixed clock keeps "12 minutes ago" stable.
const FIXED_NOW = new Date('2026-06-15T12:00:00Z');

type Open = {
	// Fake timers that never advance: mock API calls stay pending. The demo
	// session is always signed in, so /login and /setup would otherwise
	// redirect to the dashboard before they could be captured.
	frozen?: boolean;
	session?: Record<string, string>; // sessionStorage entries
};

// One entry per SvelteKit route; `route` is the route's directory pattern.
const ROUTES: ({ name: string; route: string; path: string } & Open)[] = [
	{ name: 'dashboard', route: '/', path: '/' },
	{ name: 'activity', route: '/activity', path: '/activity' },
	{ name: 'backups', route: '/backups', path: '/backups' },
	{ name: 'backup-run', route: '/backups/[runId]', path: '/backups/run-demo-1' },
	{ name: 'databases', route: '/databases', path: '/databases' },
	{ name: 'login', route: '/login', path: '/login', frozen: true },
	{ name: 'logs', route: '/logs', path: '/logs' },
	{ name: 'restore', route: '/restore', path: '/restore' },
	{ name: 'restore-browse', route: '/restore/browse', path: '/restore/browse' },
	{ name: 'restore-plan', route: '/restore/plan', path: '/restore/plan' },
	{ name: 'settings', route: '/settings', path: '/settings' },
	{ name: 'settings-directories', route: '/settings/directories', path: '/settings/directories' },
	{ name: 'settings-general', route: '/settings/general', path: '/settings/general' },
	{ name: 'settings-jobs', route: '/settings/jobs', path: '/settings/jobs' },
	{ name: 'settings-monitoring', route: '/settings/monitoring', path: '/settings/monitoring' },
	{ name: 'settings-notifications', route: '/settings/notifications', path: '/settings/notifications' },
	{ name: 'settings-recovery-kit', route: '/settings/recovery-kit', path: '/settings/recovery-kit' },
	{ name: 'settings-schedule', route: '/settings/schedule', path: '/settings/schedule' },
	{ name: 'settings-security', route: '/settings/security', path: '/settings/security' },
	{ name: 'settings-targets', route: '/settings/targets', path: '/settings/targets' },
	...[1, 2, 3, 4, 5].map((step) => ({
		name: `setup-step-${step}`,
		route: '/setup',
		path: '/setup',
		frozen: true,
		session: { arkive_setup_step: String(step) },
	})),
	{ name: 'snapshots', route: '/snapshots', path: '/snapshots' },
	{ name: 'snapshot-detail', route: '/snapshots/[id]', path: '/snapshots/abc12345' },
	{ name: 'not-found', route: '+error', path: '/no-such-page' },
];

// Opened panels, forms and dialogs, which plain page loads don't show.
const STATES: ({ name: string; path: string; phoneOnly?: boolean; act: (page: Page) => Promise<void> } & Open)[] = [
	{
		name: 'command-palette',
		path: '/',
		act: async (page) => {
			await page.keyboard.press('Control+k');
			await expect(page.getByRole('dialog').first()).toBeVisible();
		},
	},
	{
		name: 'targets-add-form',
		path: '/settings/targets',
		act: (page) => page.getByRole('button', { name: 'Add Target' }).click(),
	},
	{
		name: 'targets-delete-dialog',
		path: '/settings/targets',
		act: async (page) => {
			await page.getByRole('button', { name: 'Delete target' }).first().click();
			await expect(page.getByRole('dialog').first()).toBeVisible();
		},
	},
	{
		name: 'notifications-add-form',
		path: '/settings/notifications',
		act: (page) => page.getByRole('button', { name: 'Add Channel' }).click(),
	},
	{
		name: 'focus-form-input',
		path: '/settings/general',
		act: (page) => page.locator('#server-name').focus(),
	},
	{
		name: 'focus-login',
		path: '/login',
		frozen: true,
		act: (page) => page.locator('#api-key').focus(),
	},
	{
		name: 'focus-keyboard',
		path: '/settings/targets',
		act: async (page) => {
			for (let i = 0; i < 4; i++) await page.keyboard.press('Tab');
		},
	},
	{
		name: 'sidebar-open',
		path: '/settings/general',
		phoneOnly: true, // the toggle is hidden at lg and up
		act: (page) => page.getByRole('button', { name: 'Toggle sidebar' }).click(),
	},
	// The drill results table: the app's only table whose cells rely on the
	// browser's default cell padding.
	...['passed', 'failed'].map((status, row) => ({
		name: `drill-${status}`,
		path: '/',
		act: async (page: Page) => {
			const panel = page.getByTestId('restore-drills-panel');
			await panel.locator('button[aria-expanded]').nth(row).click();
			await expect(panel.locator('table')).toBeVisible();
		},
	})),
];

// Colours set through color-mix() compute to color(srgb r g b) and plain ones
// to rgb(); write both as 8-bit rgb() so only a different colour counts.
function rgb(text: string): string {
	return text.replace(/color\(srgb ([\d.e-]+) ([\d.e-]+) ([\d.e-]+)(?: \/ ([\d.]+))?\)/g, (_, r, g, b, alpha) => {
		const [red, green, blue] = [r, g, b].map((v: string) => Math.round(Number(v) * 255));
		return alpha === undefined || alpha === '1'
			? `rgb(${red}, ${green}, ${blue})`
			: `rgba(${red}, ${green}, ${blue}, ${alpha})`;
	});
}

// Native <select> option lists open outside the page, so no screenshot shows
// them. Compare the computed style of every <option> on the page instead.
async function expectOptionsToMatch(page: Page, name: string) {
	const options = await page.locator('option').evaluateAll((elements) =>
		elements.map((el) => {
			const s = getComputedStyle(el);
			const select = el.closest('select');
			const owner = select?.id || select?.getAttribute('aria-label') || 'select';
			return `${owner} > ${el.textContent?.trim()}: padding ${s.padding}; margin ${s.margin}; font ${s.font}; color ${s.color}; background ${s.backgroundColor}`;
		}),
	);
	expect.soft(rgb(options.join('\n')) || '(no options)').toMatchSnapshot(`${name}.options.txt`);
}

async function open(page: Page, path: string, theme: string, { frozen, session }: Open = {}) {
	if (frozen) {
		await page.clock.install({ time: new Date(FIXED_NOW.getTime() - 1000) });
		await page.clock.pauseAt(FIXED_NOW);
	} else {
		await page.clock.setFixedTime(FIXED_NOW);
	}
	await page.addInitScript(
		([t, entries]) => {
			try {
				localStorage.setItem('arkive_theme', t);
				for (const [key, value] of Object.entries(entries)) sessionStorage.setItem(key, value);
			} catch {
				// storage unavailable: the app falls back to dark
			}
		},
		[theme, session ?? {}] as const,
	);
	await page.goto(`${path}?demo=true`);
	await page.waitForLoadState('networkidle');
	await page.evaluate(() => document.fonts.ready);
	// Route transitions and in-app redirects (e.g. /restore) settle.
	await page.waitForTimeout(500);
}

function themeOf(metadata: Record<string, unknown>): string {
	return metadata.theme === 'light' ? 'light' : 'dark';
}

test('every route is listed', ({}, testInfo) => {
	test.skip(testInfo.project.name !== 'desktop-dark', 'checked once');
	const routesDir = fileURLToPath(new URL('../../src/routes', import.meta.url));
	const found = (readdirSync(routesDir, { recursive: true }) as string[])
		.filter((f) => f.endsWith('+page.svelte'))
		.map((f) => '/' + f.replace(/\\/g, '/').replace(/\/?\+page\.svelte$/, ''));
	const listed = new Set(ROUTES.map((r) => r.route));
	expect(found.filter((r) => !listed.has(r))).toEqual([]);
});

for (const { name, path, ...opts } of ROUTES) {
	test(`route ${name}`, async ({ page }, testInfo) => {
		await open(page, path, themeOf(testInfo.project.metadata), opts);
		await expect.soft(page).toHaveScreenshot(`${name}.png`, { fullPage: true });
		await expectOptionsToMatch(page, name);
	});
}

for (const { name, path, phoneOnly, act, ...opts } of STATES) {
	test(`state ${name}`, async ({ page }, testInfo) => {
		test.skip(!!phoneOnly && !testInfo.project.name.startsWith('phone'), 'phone layout only');
		await open(page, path, themeOf(testInfo.project.metadata), opts);
		await act(page);
		await page.waitForTimeout(400);
		await expect.soft(page).toHaveScreenshot(`${name}.png`, { fullPage: true });
		await expectOptionsToMatch(page, name);
	});
}
