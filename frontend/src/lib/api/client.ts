import { mockApi } from './mock';

const BASE_URL = '/api';

// Enable demo mode only when explicitly requested via ?demo=true
const DEMO_MODE = typeof window !== 'undefined' &&
	window.location.search.includes('demo=true');

export interface BrowserSession {
	setup_required: boolean;
	authenticated?: boolean;
	setup_completed_at?: string;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
	const headers: Record<string, string> = {
		'Content-Type': 'application/json',
		...(options.headers as Record<string, string> || {})
	};

	const res = await fetch(`${BASE_URL}${path}`, {
		...options,
		credentials: 'include',
		headers
	});

	if (!res.ok) {
		const error = await res.json().catch(() => ({ message: res.statusText }));
		throw new ApiError(res.status, error.message || res.statusText);
	}

	return res.json();
}

export class ApiError extends Error {
	constructor(public status: number, message: string) {
		super(message);
		this.name = 'ApiError';
	}
}

const realApi = {
	get: <T>(path: string) => request<T>(path),
	post: <T>(path: string, data?: unknown) =>
		request<T>(path, {
			method: 'POST',
			body: data !== undefined ? JSON.stringify(data) : undefined
		}),
	put: <T>(path: string, data?: unknown) =>
		request<T>(path, {
			method: 'PUT',
			body: data !== undefined ? JSON.stringify(data) : undefined
		}),
	delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

	getStatus: () => request<any>('/status'),

	getSession: () => request<BrowserSession>('/auth/session'),
	login: (apiKey: string) =>
		request<BrowserSession>('/auth/login', {
			method: 'POST',
			body: JSON.stringify({ api_key: apiKey })
		}),
	logout: () => request<{ authenticated: false; message: string }>('/auth/logout', { method: 'POST' }),
	completeSetup: (data: any) => request<any>('/auth/setup', { method: 'POST', body: JSON.stringify(data) }),
	rotateKey: () => request<any>('/auth/rotate-key', { method: 'POST' }),

	listJobs: () => request<any>('/jobs'),
	getJob: (id: string) => request<any>(`/jobs/${id}`),
	createJob: (data: any) => request<any>('/jobs', { method: 'POST', body: JSON.stringify(data) }),
	updateJob: (id: string, data: any) => request<any>(`/jobs/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
	deleteJob: (id: string) => request<any>(`/jobs/${id}`, { method: 'DELETE' }),
	triggerJob: (id: string) => request<any>(`/jobs/${id}/run`, { method: 'POST' }),
	listRuns: (jobId: string) => request<any>(`/jobs/${jobId}/history`),
	getRun: (jobId: string, runId: string) => request<any>(`/jobs/${jobId}/runs/${runId}`),
	cancelRun: (jobId: string, runId: string) => request<any>(`/jobs/${jobId}/runs/${runId}`, { method: 'DELETE' }),

	listTargets: () => request<any>('/targets'),
	getTarget: (id: string) => request<any>(`/targets/${id}`),
	createTarget: (data: any) => request<any>('/targets', { method: 'POST', body: JSON.stringify(data) }),
	updateTarget: (id: string, data: any) => request<any>(`/targets/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
	deleteTarget: (id: string) => request<any>(`/targets/${id}`, { method: 'DELETE' }),
	testTarget: (id: string) => request<any>(`/targets/${id}/test`, { method: 'POST' }),

	listSnapshots: (targetId?: string) => request<any>(`/snapshots${targetId ? `?target_id=${targetId}` : ''}`),
	refreshSnapshots: () => request<any>('/snapshots/refresh', { method: 'POST' }),
	browseSnapshot: (id: string, path: string = '/') => request<any>(`/snapshots/${id}/browse?path=${encodeURIComponent(path)}`),

	restore: (data: any) => request<any>('/restore', { method: 'POST', body: JSON.stringify(data) }),
	getSettings: () => request<any>('/settings'),
	updateSettings: (data: any) => request<any>('/settings', { method: 'PUT', body: JSON.stringify(data) }),

	listChannels: () => request<any>('/notifications'),
	createChannel: (data: any) => request<any>('/notifications', { method: 'POST', body: JSON.stringify(data) }),
	updateChannel: (id: string, data: any) => request<any>(`/notifications/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
	deleteChannel: (id: string) => request<any>(`/notifications/${id}`, { method: 'DELETE' }),
	testChannel: (id: string) => request<any>(`/notifications/${id}/test`, { method: 'POST' }),

	listActivity: (limit?: number, offset?: number) => {
		const params = new URLSearchParams();
		if (limit != null) params.set('limit', String(limit));
		if (offset != null) params.set('offset', String(offset));
		const qs = params.toString();
		return request<any>(`/activity${qs ? `?${qs}` : ''}`);
	},
	getStorageStats: () => request<any>('/storage'),
	runScan: () => request<any>('/discover/scan', { method: 'POST' }),
	listContainers: () => request<any>('/discover/containers'),
	listDatabases: () => request<any>('/databases'),
	dumpDatabase: (container: string, dbName: string) =>
		request<any>(`/databases/${container}/${dbName}/dump`, { method: 'POST' }),
	listDirectories: () => request<any>('/directories'),
	addDirectory: (data: any) => request<any>('/directories', { method: 'POST', body: JSON.stringify(data) }),
	updateDirectory: (id: string, data: any) => request<any>(`/directories/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
	deleteDirectory: (id: string) => request<any>(`/directories/${id}`, { method: 'DELETE' }),
	scanDirectories: () => request<any>('/directories/scan', { method: 'POST' }),
	getLogs: (lines?: number) => request<any>(`/logs${lines ? `?lines=${lines}` : ''}`),
	clearLogs: () => request<any>('/logs', { method: 'DELETE' }),

	createEventSource: () => new EventSource(`${BASE_URL}/events/stream`)
};

export const api = DEMO_MODE ? mockApi : realApi;
