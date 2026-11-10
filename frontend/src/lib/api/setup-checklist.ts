import { api } from './client';
import { normalizeChecklist, type SetupChecklist } from '$lib/utils/setup-checklist';

/** The post-setup checklist, or null when the payload isn't one (older backend). */
export async function getSetupChecklist(): Promise<SetupChecklist | null> {
	return normalizeChecklist(await api.get('/setup-checklist'));
}

/** Hide the checklist for good. Rejects with ApiError 409 until every step is done. */
export async function dismissSetupChecklist(): Promise<SetupChecklist | null> {
	return normalizeChecklist(await api.post('/setup-checklist/dismiss'));
}
