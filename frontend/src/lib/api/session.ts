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

/** Only allow same-origin relative paths as post-login destinations. */
export function safeNext(raw: string | null | undefined): string {
	if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) return '/';
	if (raw === '/login' || raw.startsWith('/login?') || raw.startsWith('/setup')) return '/';
	return raw;
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
