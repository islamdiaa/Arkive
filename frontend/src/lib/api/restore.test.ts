import { beforeEach, describe, expect, it, vi } from 'vitest';

import { buildRestoreRequest, downloadRestorePlan, interpretRestoreResult } from './restore';

describe('restore api', () => {
	beforeEach(() => {
		vi.unstubAllGlobals();
	});

	it('downloads the restore plan with cookie credentials instead of query-string auth', async () => {
		const blob = new Blob(['pdf-data'], { type: 'application/pdf' });
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			headers: new Headers({
				'Content-Type': 'application/pdf',
				'Content-Disposition': 'attachment; filename="arkive-restore-plan.pdf"',
			}),
			blob: vi.fn().mockResolvedValue(blob),
		});

		vi.stubGlobal('fetch', fetchMock);

		const result = await downloadRestorePlan();

		expect(fetchMock).toHaveBeenCalledWith('/api/restore/plan/pdf', {
			credentials: 'include',
		});
		expect(result).toEqual({ blob, filename: 'arkive-restore-plan.pdf', isPdf: true });
	});

	it('uses an .html filename when the server falls back to an HTML plan', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			headers: new Headers({ 'Content-Type': 'text/html; charset=utf-8' }),
			blob: vi.fn().mockResolvedValue(new Blob(['<html></html>'])),
		});
		vi.stubGlobal('fetch', fetchMock);

		const result = await downloadRestorePlan();
		expect(result.filename).toBe('arkive-restore-plan.html');
		expect(result.isPdf).toBe(false);
	});

	it('throws instead of saving an error body as a PDF', async () => {
		const fetchMock = vi.fn().mockResolvedValue({
			ok: false,
			status: 500,
			statusText: 'Internal Server Error',
			headers: new Headers({ 'Content-Type': 'application/json' }),
			json: vi.fn().mockResolvedValue({ message: 'PDF generation failed' }),
			blob: vi.fn(),
		});
		vi.stubGlobal('fetch', fetchMock);

		await expect(downloadRestorePlan()).rejects.toThrow('PDF generation failed');
	});
});

describe('buildRestoreRequest', () => {
	const snapshot = {
		id: 'abcd1234',
		full_id: 'abcd1234ffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
		target_id: 'tgt-1',
		paths: ['/config/dumps', '/mnt/user/appdata'],
	};

	it('matches the backend RestoreRequest contract', () => {
		expect(buildRestoreRequest(snapshot, { restoreTo: '/tmp/arkive-restore', dryRun: false })).toEqual({
			snapshot_id: snapshot.full_id,
			target: 'tgt-1',
			paths: ['/config/dumps', '/mnt/user/appdata'],
			restore_to: '/tmp/arkive-restore',
			dry_run: false,
		});
	});

	it('never sends legacy field names', () => {
		const body = buildRestoreRequest(snapshot, { restoreTo: '/tmp/arkive-restore', dryRun: true }) as unknown as Record<string, unknown>;
		expect(body).not.toHaveProperty('target_path');
		expect(body).not.toHaveProperty('target_id');
		expect(body).not.toHaveProperty('overwrite');
	});

	it('falls back to the short id and empty paths', () => {
		const body = buildRestoreRequest({ id: 'abcd1234', target_id: 't' }, { dryRun: true });
		expect(body.snapshot_id).toBe('abcd1234');
		expect(body.paths).toEqual([]);
		expect(body).not.toHaveProperty('restore_to');
	});
});

describe('interpretRestoreResult', () => {
	it('treats a dry run reported as failed (HTTP 200) as an error', () => {
		expect(interpretRestoreResult({ status: 'failed', message: 'Dry run failed: repo locked' }, true)).toEqual({
			ok: false,
			message: 'Dry run failed: repo locked',
		});
	});

	it('reports the number of entries for a successful dry run', () => {
		const outcome = interpretRestoreResult({ status: 'dry_run', entries: [{}, {}, {}] }, true);
		expect(outcome.ok).toBe(true);
		expect(outcome.message).toContain('3 entries');
	});

	it('only treats status success as a successful restore', () => {
		expect(interpretRestoreResult({ status: 'success' }, false).ok).toBe(true);
		expect(interpretRestoreResult({ status: 'failed', message: 'restic: wrong password' }, false)).toEqual({
			ok: false,
			message: 'restic: wrong password',
		});
	});
});
