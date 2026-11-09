import { defineConfig } from 'vitest/config';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';

export default defineConfig({
	// There is no svelte.config.js any more (SvelteKit 3 reads vite.config.js), so pass
	// the preprocessor the app build uses explicitly.
	plugins: [svelte({ configFile: false, preprocess: vitePreprocess() }), svelteTesting()],
	test: {
		include: ['src/**/*.{test,spec}.{js,ts}'],
		environment: 'jsdom',
		globals: true,
		setupFiles: ['src/tests/setup.ts'],
		pool: 'threads',
	},
	resolve: {
		alias: {
			$lib: new URL('./src/lib', import.meta.url).pathname,
			'$app/state': new URL('./src/tests/mocks/app/state.svelte.ts', import.meta.url).pathname,
		},
	},
});
