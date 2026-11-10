import { page as pageState, type Page } from '$app/state';
import { toStore, type Readable } from 'svelte/store';

/**
 * `$page` for components that still use legacy reactivity (`$:`).
 *
 * SvelteKit 3 removed `$app/stores`. Its replacement, `page` from `$app/state`,
 * only notifies runes: a `$: path = page.url.pathname` statement never re-runs
 * after the first navigation. This store re-exposes `$app/state` as a store so
 * `$page.url.pathname` keeps updating in those components. Components written
 * with runes should import `page` from `$app/state` directly.
 */
export type PageSnapshot = Pick<Page, 'url' | 'params' | 'route' | 'status' | 'error' | 'data' | 'state'>;

function snapshot(): PageSnapshot {
	return {
		url: pageState.url,
		params: pageState.params,
		route: pageState.route,
		status: pageState.status,
		error: pageState.error,
		data: pageState.data,
		state: pageState.state,
	};
}

export const page: Readable<PageSnapshot> = {
	// Read `$app/state` only once something subscribes (during component init),
	// never at module load. Each subscription gets its own effect root, which
	// lives until that subscriber unsubscribes.
	subscribe: (run, invalidate) => toStore(snapshot).subscribe(run, invalidate),
};
