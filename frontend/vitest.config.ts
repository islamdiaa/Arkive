import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';

export default defineConfig({
	plugins: [svelte(), svelteTesting()],
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
			'$app/stores': new URL('./src/tests/mocks/app/stores.ts', import.meta.url).pathname,
		},
	},
});
