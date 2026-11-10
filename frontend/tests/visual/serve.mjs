// Serve a static SvelteKit build (adapter-static, SPA fallback) for the
// visual comparison. Usage: node tests/visual/serve.mjs <build-dir> <port>
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve, sep } from 'node:path';

const root = resolve(process.argv[2] ?? 'build');
const port = Number(process.argv[3] ?? 5320);

/** @type {Record<string, string>} */
const TYPES = {
	'.html': 'text/html; charset=utf-8',
	'.js': 'text/javascript; charset=utf-8',
	'.css': 'text/css; charset=utf-8',
	'.json': 'application/json',
	'.svg': 'image/svg+xml',
	'.png': 'image/png',
	'.ico': 'image/x-icon',
	'.woff': 'font/woff',
	'.woff2': 'font/woff2',
};

/** @param {string} urlPath */
async function fileFor(urlPath) {
	const path = normalize(join(root, decodeURIComponent(urlPath)));
	if (path !== root && !path.startsWith(root + sep)) return null;
	try {
		if ((await stat(path)).isFile()) return path;
	} catch {
		// not a file: fall through to the SPA fallback
	}
	return null;
}

createServer(async (req, res) => {
	const { pathname } = new URL(req.url ?? '/', 'http://localhost');
	// Demo mode needs no backend; an API call must not get index.html back.
	if (pathname.startsWith('/api/')) {
		res.writeHead(404).end();
		return;
	}
	const path = (await fileFor(pathname)) ?? join(root, 'index.html');
	try {
		const body = await readFile(path);
		res.writeHead(200, { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream' });
		res.end(body);
	} catch {
		res.writeHead(404).end();
	}
}).listen(port, '127.0.0.1', () => {
	console.log(`serving ${root} on http://127.0.0.1:${port}`);
});
