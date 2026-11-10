import { afterEach, describe, expect, it, vi } from 'vitest';

import { goToNext, loginUrl, navigation, safeNext } from './session';

// Each of these resolves to another origin in a browser: the URL parser drops
// tab/CR/LF and reads '\' as '/'.
const OFF_ORIGIN = [
	'/\t/evil.example/phish',
	'/\n/evil.example',
	'/\r\\evil.example',
	'/\t\\evil.example',
	'/\r\n/evil.example',
];

// Same origin as typed, but the parsed path starts with '//' ('%2e' is a dot
// segment too), so it must not be handed back as a string.
const DOUBLE_SLASH_PATH = ['/.//evil.example', '/%2e//evil.example', '/./\\evil.example'];

describe('safeNext', () => {
	it('accepts same-origin relative paths', () => {
		expect(safeNext('/backups')).toBe('/backups');
		expect(safeNext('/settings/jobs?x=1')).toBe('/settings/jobs?x=1');
		expect(safeNext('/snapshots/abc#files')).toBe('/snapshots/abc#files');
	});

	it('rejects absolute, protocol-relative and login/setup destinations', () => {
		expect(safeNext('https://evil.example')).toBe('/');
		expect(safeNext('//evil.example')).toBe('/');
		expect(safeNext('/\\evil.example')).toBe('/');
		expect(safeNext('/login')).toBe('/');
		expect(safeNext('/login?next=/backups')).toBe('/');
		expect(safeNext('/./login')).toBe('/');
		expect(safeNext('/setup')).toBe('/');
		expect(safeNext(null)).toBe('/');
	});

	it.each(OFF_ORIGIN)('rejects %j, which the browser resolves off-site', (raw) => {
		expect(new URL(raw, 'http://tower:8200/login').origin).not.toBe('http://tower:8200');
		expect(safeNext(raw)).toBe('/');
	});

	it.each(DOUBLE_SLASH_PATH)('rejects %j, whose parsed path is protocol-relative', (raw) => {
		expect(new URL(raw, 'http://tower:8200/login').pathname).toBe('//evil.example');
		expect(safeNext(raw)).toBe('/');
	});

	it('hands back the path the browser would load, without the dropped characters', () => {
		expect(safeNext('/back\tups')).toBe('/backups');
		expect(safeNext('/settings/../backups')).toBe('/backups');
	});
});

describe('loginUrl', () => {
	it('omits next for the dashboard and encodes other paths', () => {
		expect(loginUrl('/')).toBe('/login');
		expect(loginUrl('/snapshots')).toBe('/login?next=%2Fsnapshots');
	});

	it('drops an off-site next', () => {
		expect(loginUrl('/\t/evil.example')).toBe('/login');
	});
});

describe('goToNext', () => {
	const startUrl = window.location.href;

	afterEach(() => {
		window.history.replaceState(null, '', startUrl);
		vi.restoreAllMocks();
	});

	function onLogin(next: string) {
		window.history.replaceState(null, '', `/login?next=${encodeURIComponent(next)}`);
	}

	function sameOrigin(url: string): boolean {
		return new URL(url, window.location.href).origin === window.location.origin;
	}

	it('navigates to next with goto', async () => {
		onLogin('/backups');
		const go = vi.fn().mockResolvedValue(undefined);
		const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {});

		await goToNext(go);

		expect(go).toHaveBeenCalledWith('/backups');
		expect(assign).not.toHaveBeenCalled();
	});

	// SvelteKit 3's goto() rejects a path with no route; load it like a link so
	// the user sees the 404 page instead of staying on /login.
	it('loads next directly when goto rejects', async () => {
		onLogin('/no-such-page');
		const go = vi.fn().mockRejectedValue(new Error('navigation_route_missing'));
		const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {});

		await goToNext(go);

		expect(assign).toHaveBeenCalledWith('/no-such-page');
	});

	// goto() also rejects a URL on another origin; the fallback must not turn
	// that into a full-page redirect off-site.
	it.each([...OFF_ORIGIN, ...DOUBLE_SLASH_PATH])('never leaves the origin for next=%j', async (raw) => {
		onLogin(raw);
		const go = vi.fn().mockRejectedValue(new Error('navigation_external_url'));
		const assign = vi.spyOn(navigation, 'assign').mockImplementation(() => {});

		await goToNext(go);

		expect(go).toHaveBeenCalledWith('/');
		for (const [url] of [...go.mock.calls, ...assign.mock.calls]) {
			expect(sameOrigin(url)).toBe(true);
		}
	});
});
