import { z } from 'zod';
import { FALLBACK_NOTIFICATION_EVENTS, WILDCARD_EVENT } from '$lib/utils/notification-events';

/**
 * Build a channel schema for a given event catalog. Pass the values from
 * GET /api/notifications/events; the backend rejects anything else with 422.
 */
export function createNotificationSchema(eventValues: readonly string[]) {
  const allowed = new Set([...eventValues, WILDCARD_EVENT]);
  return z.object({
    name: z.string().min(1).max(50),
    type: z.enum(['slack', 'discord', 'telegram', 'email', 'ntfy', 'gotify', 'pushover', 'webhook', 'uptimekuma']),
    url: z.string().min(1, 'Notification URL is required'),
    events: z
      .array(z.string().refine((v) => allowed.has(v), { message: 'Unknown event type' }))
      .min(1, 'Select at least one event'),
    enabled: z.boolean().default(true),
  });
}

/** Schema against the built-in fallback catalog. */
export const notificationSchema = createNotificationSchema(FALLBACK_NOTIFICATION_EVENTS.map((e) => e.value));
