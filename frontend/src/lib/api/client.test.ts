import { beforeEach, describe, expect, it, vi } from 'vitest';

async function loadClientModule() {
	vi.resetModules();
	return import('./client');
}

describe('api client', () => {
	beforeEach(() => {
		vi.unstubAllGlobals();
	});

	it('sends cookie-authenticated requests with browser credentials', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ status: 'ok' }), {
				status: 200,
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api } = await loadClientModule();
		const result = await api.getStatus();

		expect(result).toEqual({ status: 'ok' });
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/status',
			expect.objectContaining({
				credentials: 'include',
				headers: expect.objectContaining({
					'Content-Type': 'application/json',
				}),
			})
		);
	});

	it('does not inject a legacy API key header', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ status: 'ok' }), {
				status: 200,
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api } = await loadClientModule();
		await api.getStatus();

		expect(fetchMock).toHaveBeenCalledWith(
			'/api/status',
			expect.objectContaining({
				credentials: 'include',
				headers: expect.not.objectContaining({
					'X-API-Key': expect.any(String),
				}),
			})
		);
	});

	it('posts JSON login requests with browser credentials', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ authenticated: true }), {
				status: 200,
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api } = await loadClientModule();
		await api.login('ark_login_key');

		expect(fetchMock).toHaveBeenCalledWith(
			'/api/auth/login',
			expect.objectContaining({
				method: 'POST',
				credentials: 'include',
				body: JSON.stringify({ api_key: 'ark_login_key' }),
			})
		);
	});

	it('throws ApiError with the backend message when JSON error payload is present', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ message: 'Nope' }), {
				status: 401,
				statusText: 'Unauthorized',
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api, ApiError } = await loadClientModule();

		await expect(api.getStatus()).rejects.toEqual(new ApiError(401, 'Nope'));
	});

	it('surfaces a 400 restore error message verbatim (read-only destination)', async () => {
		const detail = "Restore destination /mnt/user/restore is on a read-only mount; choose a writable folder.";
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ error: 'bad_request', message: detail, details: {} }), {
				status: 400,
				statusText: 'Bad Request',
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api, ApiError } = await loadClientModule();

		await expect(api.restore({ snapshot_id: 'abc', target: 't1', paths: [] })).rejects.toEqual(new ApiError(400, detail));
	});

	it('falls back to a FastAPI-style detail string when there is no message', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ detail: 'A restore drill is already running' }), {
				status: 409,
				statusText: 'Conflict',
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api, ApiError } = await loadClientModule();

		await expect(api.post('/drills/run')).rejects.toEqual(new ApiError(409, 'A restore drill is already running'));
	});

	it('redirects to login with a next parameter when an authenticated session gets a 401', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ message: 'Missing API key' }), {
				status: 401,
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api, ApiError } = await loadClientModule();
		const auth = await import('$lib/stores/auth');
		const session = await import('./session');
		const assign = vi.spyOn(session.navigation, 'assign').mockImplementation(() => {});
		auth.applySession({ setup_required: false, authenticated: true });
		window.history.pushState({}, '', '/backups?tab=1');

		await expect(api.listJobs()).rejects.toEqual(new ApiError(401, 'Missing API key'));

		expect(assign).toHaveBeenCalledWith('/login?next=%2Fbackups%3Ftab%3D1');
		const { get } = await import('svelte/store');
		expect(get(auth.authenticated)).toBe(false);
		window.history.pushState({}, '', '/');
	});

	it('does not redirect on a 401 from the login endpoint', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(JSON.stringify({ message: 'Invalid API key' }), {
				status: 401,
				headers: { 'Content-Type': 'application/json' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api } = await loadClientModule();
		const auth = await import('$lib/stores/auth');
		const session = await import('./session');
		const assign = vi.spyOn(session.navigation, 'assign').mockImplementation(() => {});
		auth.applySession({ setup_required: false, authenticated: true });

		await expect(api.login('bad')).rejects.toThrow('Invalid API key');
		expect(assign).not.toHaveBeenCalled();
	});

	it('falls back to the HTTP status text when the error body is not JSON', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response('bad gateway', {
				status: 502,
				statusText: 'Bad Gateway',
				headers: { 'Content-Type': 'text/plain' },
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const { api, ApiError } = await loadClientModule();

		await expect(api.getStatus()).rejects.toEqual(new ApiError(502, 'Bad Gateway'));
	});
});
