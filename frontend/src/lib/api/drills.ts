import { api } from './client';
import type { PaginatedResponse, RestoreDrill, RestoreDrillDetail } from '$lib/types';

export async function listDrills(limit = 20, offset = 0): Promise<PaginatedResponse<RestoreDrill>> {
  const result: any = await api.get(`/drills?limit=${limit}&offset=${offset}`);
  return {
    items: Array.isArray(result?.items) ? result.items : [],
    total: typeof result?.total === 'number' ? result.total : 0,
  };
}

export async function getDrill(id: string): Promise<RestoreDrillDetail> {
  const result: any = await api.get(`/drills/${encodeURIComponent(id)}`);
  return { ...result, results: Array.isArray(result?.results) ? result.results : [] };
}

/** Start a drill now. Rejects with ApiError 409 if one is running, 503 if unavailable. */
export async function runDrill(): Promise<{ drill_id: string; status: string }> {
  return api.post('/drills/run');
}
