import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

class FakeEventSource {
	static instances: FakeEventSource[] = [];
	url: string;
	onopen: (() => void) | null = null;
	onmessage: ((e: MessageEvent) => void) | null = null;
	onerror: (() => void) | null = null;
	closed = false;
	constructor(url: string) {
		this.url = url;
		FakeEventSource.instances.push(this);
	}
	addEventListener() {}
	close() {
		this.closed = true;
	}
}

async function loadSse() {
	vi.resetModules();
	return import('./sse');
}

function tokenResponse() {
	return new Response(JSON.stringify({ token: 'tok' }), {
		status: 200,
		headers: { 'Content-Type': 'application/json' },
	});
}

describe('sse store reconnect', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		FakeEventSource.instances = [];
		vi.stubGlobal('EventSource', FakeEventSource);
	});

	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
	});

	it('keeps retrying when the token request fails with a network error', async () => {
		const fetchMock = vi
			.fn()
			.mockRejectedValueOnce(new TypeError('Failed to fetch'))
			.mockRejectedValueOnce(new TypeError('Failed to fetch'))
			.mockResolvedValue(tokenResponse());
		vi.stubGlobal('fetch', fetchMock);

		const sse = await loadSse();
		await sse.connect();
		expect(FakeEventSource.instances).toHaveLength(0);
		expect(get(sse.reconnectAttempts)).toBe(1);

		await vi.advanceTimersByTimeAsync(1000);
		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(get(sse.reconnectAttempts)).toBe(2);

		await vi.advanceTimersByTimeAsync(2000);
		expect(fetchMock).toHaveBeenCalledTimes(3);
		expect(FakeEventSource.instances).toHaveLength(1);
		expect(FakeEventSource.instances[0].url).toContain('token=tok');

		FakeEventSource.instances[0].onopen?.();
		expect(get(sse.connected)).toBe(true);
		expect(get(sse.reconnectAttempts)).toBe(0);
		sse.disconnect();
	});

	it('retries on non-401 HTTP errors such as 502 from a proxy', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(new Response('bad gateway', { status: 502 }))
			.mockResolvedValue(tokenResponse());
		vi.stubGlobal('fetch', fetchMock);
		vi.spyOn(console, 'warn').mockImplementation(() => {});

		const sse = await loadSse();
		await sse.connect();
		await vi.advanceTimersByTimeAsync(1000);

		expect(FakeEventSource.instances).toHaveLength(1);
		sse.disconnect();
	});

	it('reconnects after the stream errors, and stops after disconnect', async () => {
		const fetchMock = vi.fn().mockImplementation(async () => tokenResponse());
		vi.stubGlobal('fetch', fetchMock);

		const sse = await loadSse();
		await sse.connect();
		expect(FakeEventSource.instances).toHaveLength(1);

		FakeEventSource.instances[0].onerror?.();
		expect(FakeEventSource.instances[0].closed).toBe(true);
		await vi.advanceTimersByTimeAsync(1000);
		expect(FakeEventSource.instances).toHaveLength(2);

		sse.disconnect();
		FakeEventSource.instances[1].onerror?.();
		await vi.advanceTimersByTimeAsync(60000);
		expect(FakeEventSource.instances).toHaveLength(2);
	});

	it('listens for restore drill events and dispatches them to subscribers', async () => {
		vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => tokenResponse()));
		const listeners = new Map<string, (e: MessageEvent) => void>();
		const addEventListener = vi
			.spyOn(FakeEventSource.prototype, 'addEventListener')
			.mockImplementation(function (name: string, fn: (e: MessageEvent) => void) {
				listeners.set(name, fn);
			} as any);

		const sse = await loadSse();
		const received: unknown[] = [];
		const unsub = sse.subscribe('drill:completed', (event) => received.push(event.data));
		await sse.connect();

		expect(listeners.has('drill:started')).toBe(true);
		expect(listeners.has('drill:completed')).toBe(true);
		listeners.get('drill:completed')!({ data: JSON.stringify({ drill_id: 'd1', status: 'passed' }) } as MessageEvent);
		expect(received).toEqual([{ drill_id: 'd1', status: 'passed' }]);

		unsub();
		addEventListener.mockRestore();
		sse.disconnect();
	});

	it('does not retry when the session is no longer valid (401)', async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }));
		vi.stubGlobal('fetch', fetchMock);

		const sse = await loadSse();
		await sse.connect();
		await vi.advanceTimersByTimeAsync(60000);

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(FakeEventSource.instances).toHaveLength(0);
	});
});
