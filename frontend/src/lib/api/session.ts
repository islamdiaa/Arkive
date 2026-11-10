/**
 * Browser-session helpers: expired-session handling and login redirects.
 */
import { get } from 'svelte/store';
import { authenticated, clearSession } from '$lib/stores/auth';

/** Indirection so tests can observe navigation without jsdom page loads. */
export const navigation = {
	assign(url: string) {
		window.location.assign(url);
	},
};

let redirecting = false;

// Stand-in origin for parsing ?next=: a path that resolves anywhere else would
// leave Arkive's origin too, wherever Arkive is served.
const PARSE_ORIGIN = 'http://arkive.invalid';

/**
 * Only allow same-origin paths as post-login destinations. Returns the path
 * as the URL parser reads it, or '/'.
 *
 * Prefix checks are not enough: browsers drop tab, CR and LF and read '\' as
 * '/' while parsing, so '/\t/evil.example' means '//evil.example'. Parse it the
 * same way and compare origins instead.
 */
export function safeNext(raw: string | null | undefined): string {
	if (!raw || !raw.startsWith('/')) return '/';
	let url: URL;
	try {
		url = new URL(raw, PARSE_ORIGIN);
	} catch {
		return '/';
	}
	if (url.origin !== PARSE_ORIGIN) return '/';
	const path = url.pathname + url.search + url.hash;
	// A dot segment can leave '//' at the start ('/.//evil.example'), which
	// would be protocol-relative once handed back as a string.
	if (path.startsWith('//')) return '/';
	if (url.pathname === '/login' || url.pathname.startsWith('/setup')) return '/';
	return path;
}

/**
 * Leaves /login for its ?next= destination using `go` (SvelteKit's goto).
 *
 * SvelteKit 3's goto() rejects a path that matches no route. Load the path the
 * way a link would instead, so the user lands on the 404 page rather than
 * staying on /login. goto() also rejects a URL on another origin; the fallback
 * re-checks the origin so it can never turn that into an off-site redirect.
 */
export async function goToNext(go: (url: string) => Promise<unknown>): Promise<void> {
	const next = safeNext(new URLSearchParams(window.location.search).get('next'));
	try {
		await go(next);
	} catch {
		if (new URL(next, window.location.href).origin !== window.location.origin) return;
		navigation.assign(next);
	}
}

export function loginUrl(next?: string | null): string {
	const target = safeNext(next);
	return target === '/' ? '/login' : `/login?next=${encodeURIComponent(target)}`;
}

/**
 * Called when an authenticated API request comes back 401 (session expired,
 * or the API key was rotated elsewhere). Clears local auth state and sends
 * the user to the login page, returning them to the current page afterwards.
 *
 * No-op when we never believed we had a session: the layout's bootstrap
 * already routes unauthenticated users to /login or /setup.
 */
export function handleUnauthorized(): void {
	if (typeof window === 'undefined' || redirecting) return;
	if (!get(authenticated)) return;
	const path = window.location.pathname;
	if (path === '/login' || path === '/setup') return;
	redirecting = true;
	clearSession();
	navigation.assign(loginUrl(path + window.location.search));
}

/** Test helper. */
export function _resetSessionRedirectForTests(): void {
	redirecting = false;
}
