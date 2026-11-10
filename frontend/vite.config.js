import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		// SvelteKit 3 reads its config from here; svelte.config.js is no longer loaded.
		sveltekit({
			preprocess: vitePreprocess(),
			adapter: adapter({
				pages: 'build',
				assets: 'build',
				fallback: 'index.html',
				precompress: false,
				strict: false
			}),
			// SvelteKit 3 no longer generates `$lib`. Keep it as an alias so this upgrade
			// doesn't have to rewrite every import; moving to `#lib` subpath imports is a
			// separate change.
			alias: {
				$lib: 'src/lib'
			}
		})
	],
	server: {
		allowedHosts: true,
		proxy: {
			'/api': 'http://localhost:8200'
		}
	}
});
