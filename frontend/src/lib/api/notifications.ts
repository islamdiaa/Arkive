import { api } from './client';
import type { NotificationChannel, NotificationEventCatalog, PaginatedResponse } from '$lib/types';
import { fallbackEventCatalog, normalizeEventCatalog } from '$lib/utils/notification-events';

export async function listChannels(): Promise<PaginatedResponse<NotificationChannel>> {
  return api.get('/notifications');
}

export async function createChannel(data: Record<string, unknown>): Promise<NotificationChannel> {
  return api.post('/notifications', data);
}

export async function updateChannel(id: string, data: Record<string, unknown>): Promise<NotificationChannel> {
  return api.put(`/notifications/${id}`, data);
}

export async function deleteChannel(id: string): Promise<void> {
  return api.delete(`/notifications/${id}`);
}

export async function testChannel(id: string): Promise<{ success: boolean; message: string }> {
  return api.post(`/notifications/${id}/test`);
}

/**
 * Load the notification event catalog. Never throws: if the request fails the
 * static fallback copy is returned with `fromFallback: true`.
 */
export async function loadNotificationEvents(): Promise<NotificationEventCatalog & { fromFallback: boolean }> {
  try {
    const raw = await api.get('/notifications/events');
    const catalog = normalizeEventCatalog(raw);
    const usable = Array.isArray((raw as any)?.events) && (raw as any).events.length > 0;
    return { ...catalog, fromFallback: !usable };
  } catch (e) {
    console.warn('Could not load notification events; using built-in list', e);
    return { ...fallbackEventCatalog(), fromFallback: true };
  }
}
