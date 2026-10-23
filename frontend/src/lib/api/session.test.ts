import { describe, expect, it } from 'vitest';

import { loginUrl, safeNext } from './session';

describe('safeNext', () => {
	it('accepts same-origin relative paths', () => {
		expect(safeNext('/backups')).toBe('/backups');
		expect(safeNext('/settings/jobs?x=1')).toBe('/settings/jobs?x=1');
	});

	it('rejects absolute, protocol-relative and login/setup destinations', () => {
		expect(safeNext('https://evil.example')).toBe('/');
		expect(safeNext('//evil.example')).toBe('/');
		expect(safeNext('/\\evil.example')).toBe('/');
		expect(safeNext('/login')).toBe('/');
		expect(safeNext('/setup')).toBe('/');
		expect(safeNext(null)).toBe('/');
	});
});

describe('loginUrl', () => {
	it('omits next for the dashboard and encodes other paths', () => {
		expect(loginUrl('/')).toBe('/login');
		expect(loginUrl('/snapshots')).toBe('/login?next=%2Fsnapshots');
	});
});
