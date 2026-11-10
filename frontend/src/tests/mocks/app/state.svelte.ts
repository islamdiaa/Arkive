// Stand-in for SvelteKit's `$app/state` in unit tests (aliased in vitest.config.ts).
// `page` is reactive like the real one, so a test can navigate by assigning
// `page.url` and components update.
export const page = $state({
	url: new URL('http://localhost'),
	params: {} as Record<string, string>,
	route: { id: null as string | null },
	status: 200,
	error: null as App.Error | null,
	data: {} as Record<string, any>,
	state: {} as App.PageState,
	form: null as any,
});

export const navigating = {
	from: null,
	to: null,
	type: null,
	willUnload: null,
	delta: null,
	complete: null,
};

export const updated = {
	current: false,
	check: async () => false,
};
