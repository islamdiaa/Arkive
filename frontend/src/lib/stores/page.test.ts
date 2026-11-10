import { describe, it, expect, afterEach } from 'vitest';
import { flushSync } from 'svelte';
import { get } from 'svelte/store';
// The `$app/state` stand-in that vitest.config.ts aliases; imported by path so
// the test can move it the way a SvelteKit navigation would.
import { page as appState } from '../../tests/mocks/app/state.svelte';
import { page } from './page';

describe('page store (legacy $page over $app/state)', () => {
	afterEach(() => {
		appState.url = new URL('http://localhost');
		appState.params = {};
		appState.status = 200;
		appState.error = null;
	});

	it('reads the current page from $app/state', () => {
		appState.url = new URL('http://localhost/backups?job=7');
		appState.params = { id: 'abc123' };

		const current = get(page);
		expect(current.url.pathname).toBe('/backups');
		expect(current.url.searchParams.get('job')).toBe('7');
		expect(current.params).toEqual({ id: 'abc123' });
	});

	it('notifies subscribers when the page changes', () => {
		const paths: string[] = [];
		const unsubscribe = page.subscribe((p) => paths.push(p.url.pathname));

		appState.url = new URL('http://localhost/snapshots');
		flushSync();
		appState.url = new URL('http://localhost/settings/general');
		flushSync();
		unsubscribe();

		expect(paths).toEqual(['/', '/snapshots', '/settings/general']);
	});

	it('passes status and error through for the error page', () => {
		const seen: Array<[number, string | undefined]> = [];
		const unsubscribe = page.subscribe((p) => seen.push([p.status, p.error?.message]));

		appState.status = 404;
		appState.error = { message: 'Not Found', status: 404 } as App.Error;
		flushSync();
		unsubscribe();

		expect(seen.at(-1)).toEqual([404, 'Not Found']);
	});

	it('stops following $app/state after the last unsubscribe', () => {
		const paths: string[] = [];
		const unsubscribe = page.subscribe((p) => paths.push(p.url.pathname));
		unsubscribe();

		appState.url = new URL('http://localhost/logs');
		flushSync();

		expect(paths).toEqual(['/']);
	});
});
