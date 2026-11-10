// @vitest-environment node
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'tailwindcss';
import { beforeAll, describe, expect, it } from 'vitest';

const appCssPath = fileURLToPath(new URL('./app.css', import.meta.url));
const appCss = readFileSync(appCssPath, 'utf8');
// tailwindcss resolves to <package>/dist/lib.js
const tailwindDir = resolve(dirname(createRequire(import.meta.url).resolve('tailwindcss')), '..');

/** Body of the first `{ ... }` block whose opening line matches `head` (no nesting inside). */
function block(css: string, head: RegExp): string {
	const match = head.exec(css);
	if (!match) throw new Error(`no block matching ${head}`);
	const start = css.indexOf('{', match.index) + 1;
	return css.slice(start, css.indexOf('}', start));
}

/** Custom properties declared in a block body, name -> value. */
function declarations(body: string): Map<string, string> {
	const out = new Map<string, string>();
	for (const m of body.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
		out.set(m[1], m[2].trim());
	}
	return out;
}

/** The rule for `selector` in compiled output, nested blocks included. */
function rule(css: string, selector: string): string {
	const start = css.indexOf(`${selector} {`);
	if (start < 0) return '';
	let depth = 0;
	for (let i = css.indexOf('{', start); i < css.length; i++) {
		if (css[i] === '{') depth++;
		if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
	}
	return '';
}

const rootTokens = declarations(block(appCss, /^\s*:root\s*\{/m));
const lightTokens = declarations(block(appCss, /^\s*\.light\s*\{/m));
const themeColors = declarations(block(appCss, /^@theme inline reference\s*\{/m));

describe('theme tokens', () => {
	it('gives every :root colour a light value', () => {
		// Aliases (var(--other)) follow the token they point at.
		const literal = [...rootTokens].filter(([, value]) => !value.startsWith('var(')).map(([name]) => name);
		expect(literal.length).toBeGreaterThan(40);
		expect(literal.filter((name) => !lightTokens.has(name))).toEqual([]);
	});

	it('only overrides tokens that exist in :root', () => {
		expect([...lightTokens.keys()].filter((name) => !rootTokens.has(name))).toEqual([]);
	});

	it('maps every Tailwind colour to a defined token', () => {
		expect(themeColors.size).toBeGreaterThan(50);
		const missing = [...themeColors].filter(([, value]) => {
			const token = /^var\((--[\w-]+)\)$/.exec(value)?.[1];
			return !token || !rootTokens.has(token);
		});
		expect(missing).toEqual([]);
	});
});

describe('compiled utilities', () => {
	let css = '';

	beforeAll(async () => {
		const compiler = await compile(appCss, {
			base: dirname(appCssPath),
			async loadStylesheet(id, base) {
				// Fonts are irrelevant here; Tailwind's own CSS comes from the package.
				if (id.startsWith('@fontsource/')) return { path: id, base, content: '' };
				const path = id === 'tailwindcss' ? resolve(tailwindDir, 'index.css') : resolve(base, id);
				return { path, base: dirname(path), content: await readFile(path, 'utf8') };
			},
		});
		css = compiler.build([
			'bg-surface',
			'bg-bg-input',
			'text-text-secondary',
			'border-border',
			'bg-primary',
			'bg-danger/10',
			'border-warning/30',
			'rounded',
			'rounded-sm',
			'text-sm',
			'shadow-md',
			'z-modal',
			'dark:bg-page',
			'outline-hidden',
		]);
	});

	it('resolves colours to the runtime theme tokens', () => {
		expect(rule(css, '.bg-surface')).toContain('background-color: var(--surface)');
		expect(rule(css, '.bg-bg-input')).toContain('background-color: var(--bg-input)');
		expect(rule(css, '.text-text-secondary')).toContain('color: var(--text-secondary)');
		expect(rule(css, '.border-border')).toContain('border-color: var(--border-default)');
		expect(rule(css, '.bg-primary')).toContain('background-color: var(--color-primary)');
	});

	it('applies opacity modifiers to token colours', () => {
		expect(rule(css, '.bg-danger\\/10')).toMatch(/color-mix\(in \w+, var\(--color-danger\) 10%, transparent\)/);
		expect(rule(css, '.border-warning\\/30')).toMatch(/color-mix\(in \w+, var\(--color-warning\) 30%, transparent\)/);
	});

	it('does not emit theme variables that shadow the tokens', () => {
		// e.g. `--color-primary: var(--color-primary)` would be a cycle.
		const emitted = [...css.matchAll(/(--color-[\w-]+)\s*:/g)].map((m) => m[1]);
		expect(emitted.filter((name) => themeColors.has(name) && !rootTokens.has(name))).toEqual([]);
		expect(css).not.toMatch(/(--color-[\w-]+)\s*:\s*var\(\1\)/);
	});

	it('keeps the Tailwind 3 scale values', () => {
		expect(rule(css, '.rounded')).toContain('border-radius: 0.25rem');
		expect(css).toContain('--radius-sm: 4px');
		expect(css).toContain('--text-sm: 0.8125rem');
		expect(rule(css, '.shadow-md')).toContain('0 2px 8px var(--tw-shadow-color, rgba(0, 0, 0, 0.3))');
		expect(css).toContain('--z-index-modal: 40');
		expect(rule(css, '.z-modal')).toContain('z-index: var(--z-index-modal)');
	});

	it('scopes dark: to [data-theme="dark"]', () => {
		expect(css).toContain('.dark\\:bg-page:where([data-theme="dark"], [data-theme="dark"] *) {');
	});

	it('keeps the browser padding on table cells and options', () => {
		// Tailwind 4's preflight zeroes it, Tailwind 3's left it; the drill results
		// table sets only some sides and relies on the 1px for the rest. In the
		// base layer, so p-*/px-* utilities still override it.
		for (const pattern of [/td,\s*th\s*\{\s*padding: 1px;\s*\}/, /\boption\s*\{\s*padding: revert;\s*\}/]) {
			const match = pattern.exec(css);
			expect(match, String(pattern)).not.toBeNull();
			const layers = [...css.slice(0, match!.index).matchAll(/@layer (\w+) \{/g)];
			expect(layers.at(-1)?.[1]).toBe('base');
		}
	});

	it('emits the component classes', () => {
		expect(rule(css, '.badge-success')).toContain('rgba(63, 185, 80, 0.15)');
		expect(css).toMatch(/\.badge-success[,\s][\s\S]*?display: inline-flex/);
		expect(rule(css, '.btn-primary')).toContain('background-color: var(--color-primary)');
	});
});
