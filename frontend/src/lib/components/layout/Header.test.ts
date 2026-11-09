import { describe, it, expect, afterEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { flushSync } from 'svelte';

// `$app/state` is aliased in vitest.config.ts to this stand-in; assigning its
// `page.url` is what a client-side navigation does.
import { page as appState } from '../../../tests/mocks/app/state.svelte';

import Header from './Header.svelte';

/** Crumb labels, without the "/" separators. */
function crumbs(): string[] {
	const nav = screen.getByRole('navigation', { name: 'Breadcrumb' });
	return Array.from(nav.children)
		.map((el) => el.textContent?.trim() ?? '')
		.filter((text) => text !== '/');
}

describe('Header breadcrumbs', () => {
	afterEach(() => {
		appState.url = new URL('http://localhost');
	});

	it('builds breadcrumbs from the current path', () => {
		appState.url = new URL('http://localhost/settings/recovery-kit');
		render(Header, { props: { title: 'Recovery Kit' } });

		expect(crumbs()).toEqual(['Home', 'Settings', 'Recovery Kit']);
	});

	// Header computes breadcrumbs in `$:` statements. Those must keep following
	// the route after SvelteKit 3 replaced `$app/stores` with `$app/state`.
	it('updates when the route changes without a reload', () => {
		appState.url = new URL('http://localhost/backups');
		render(Header, { props: { title: 'Backups' } });
		expect(crumbs()).toEqual(['Home', 'Backups']);

		appState.url = new URL('http://localhost/settings/targets');
		flushSync();

		expect(crumbs()).toEqual(['Home', 'Settings', 'Targets']);

		// and keeps following on later navigations, not just the first one
		appState.url = new URL('http://localhost/snapshots');
		flushSync();

		expect(crumbs()).toEqual(['Home', 'Snapshots']);
	});
});
