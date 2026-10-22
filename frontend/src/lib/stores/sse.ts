import { writable, get } from 'svelte/store';
import type { SSEEvent, SSEEventType } from '$lib/types';
import { handleUnauthorized } from '$lib/api/session';
import { DEMO_MODE } from '$lib/api/client';

export const connected = writable(false);
export const lastEvent = writable<SSEEvent | null>(null);
export const reconnectAttempts = writable(0);
export const events = writable<Record<string, unknown>>({});

type EventHandler = (event: SSEEvent) => void;
const handlers = new Map<SSEEventType, Set<EventHandler>>();
let eventSource: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connecting = false;  // in-flight guard for async connect
let stopped = false;     // set by disconnect(); aborts in-flight connects and retries

/**
 * Named SSE event types sent by the backend.
 *
 * The backend sets the SSE `event:` field via sse_starlette (see events.py),
 * so the browser's `onmessage` only fires for events with `event: message`.
 * All other event types require explicit `addEventListener` calls.
 */
const SSE_EVENT_NAMES: SSEEventType[] = [
  'backup:started',
  'backup:progress',
  'backup:completed',
  'backup:failed',
  'backup:phase',
  'backup:cancelled',
  'backup:db_complete',
  'backup:target_complete',
  'discovery:started',
  'discovery:progress',
  'discovery:completed',
  'restore:started',
  'restore:progress',
  'restore:completed',
  'health:changed',
  'verification:started',
  'verification:completed',
  'verification:failed',
  'drill:started',
  'drill:completed',
  'notification',
  'log:entry',
];

export function subscribe(type: SSEEventType, handler: EventHandler): () => void {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type)!.add(handler);
  return () => handlers.get(type)?.delete(handler);
}

/**
 * Dispatch a parsed SSE event to stores and registered handlers.
 */
function dispatchEvent(event: SSEEvent): void {
  lastEvent.set(event);
  events.update((prev) => {
    const next = { ...prev };
    if (event.type === 'backup:progress') next.backupProgress = event.data;
    if (event.type === 'backup:completed') next.backupCompleted = event.data;
    if (event.type === 'backup:started') next.backupStarted = event.data;
    if (event.type === 'backup:failed') next.backupFailed = event.data;
    if (event.type === 'backup:phase') next.backupPhase = event.data;
    if (event.type === 'backup:cancelled') next.backupCancelled = event.data;
    if (event.type === 'backup:db_complete') next.backupDbComplete = event.data;
    if (event.type === 'backup:target_complete') next.backupTargetComplete = event.data;
    if (event.type === 'discovery:started') next.discoveryStarted = event.data;
    if (event.type === 'discovery:progress') next.discoveryProgress = event.data;
    if (event.type === 'discovery:completed') next.discoveryCompleted = event.data;
    if (event.type === 'health:changed') next.healthChanged = event.data;
    if (event.type === 'verification:started') next.verificationStarted = event.data;
    if (event.type === 'verification:completed') next.verificationCompleted = event.data;
    if (event.type === 'verification:failed') next.verificationFailed = event.data;
    if (event.type === 'drill:started') next.drillStarted = event.data;
    if (event.type === 'drill:completed') next.drillCompleted = event.data;
    return next;
  });
  const typeHandlers = handlers.get(event.type);
  if (typeHandlers) typeHandlers.forEach(h => h(event));
}

/** Exponential backoff reconnect (1s, 2s, 4s ... capped at 30s). */
function scheduleReconnect(customUrl?: string) {
  if (stopped) return;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  const attempts = get(reconnectAttempts);
  const delay = Math.min(1000 * Math.pow(2, attempts), 30000);
  reconnectAttempts.update(n => n + 1);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect(customUrl);
  }, delay);
}

export async function connect(customUrl?: string) {
  stopped = false;
  // Demo mode runs on the mock API: there is no backend session to stream from,
  // and a 401 from the token endpoint would bounce the visitor to /login.
  if (DEMO_MODE) return;
  if (eventSource || connecting) return;

  connecting = true;
  try {
    // Fetch short-lived, single-use SSE token. Any failure other than 401
    // (server restarting, proxy 502/503, network error) schedules a retry so
    // live updates recover on their own after a container restart.
    let tokenResp: Response;
    try {
      tokenResp = await fetch('/api/auth/sse-token', {
        method: 'POST',
        credentials: 'include',
      });
    } catch {
      scheduleReconnect(customUrl);
      return;
    }
    if (stopped) return;
    if (tokenResp.status === 401) {
      // Session expired or invalidated: stop retrying and send the user to login.
      handleUnauthorized();
      return;
    }
    if (!tokenResp.ok) {
      console.warn('Failed to get SSE token:', tokenResp.status);
      scheduleReconnect(customUrl);
      return;
    }
    let sseToken = '';
    try {
      ({ token: sseToken } = await tokenResp.json());
    } catch {
      scheduleReconnect(customUrl);
      return;
    }

    // Re-check after await — another call may have connected, or we were stopped
    if (eventSource || stopped) return;

    const url = customUrl || `/api/events/stream?token=${encodeURIComponent(sseToken)}`;
    const es = new EventSource(url);
    eventSource = es;

    es.onopen = () => {
      connected.set(true);
      reconnectAttempts.set(0);
    };

    // Fallback: handles events sent without an `event:` field (type defaults to "message").
    es.onmessage = (e) => {
      try {
        const event: SSEEvent = JSON.parse(e.data);
        dispatchEvent(event);
      } catch { /* ignore parse errors */ }
    };

    // Named event listeners: the backend sends SSE events with the `event:` field
    // set to the event type (e.g., `event: backup:started`). The browser's
    // EventSource only fires `onmessage` for unnamed events (event: message).
    for (const eventName of SSE_EVENT_NAMES) {
      es.addEventListener(eventName, ((e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          const event: SSEEvent = {
            type: eventName,
            data,
            timestamp: data.timestamp || new Date().toISOString(),
          };
          dispatchEvent(event);
        } catch { /* ignore parse errors */ }
      }) as EventListener);
    }

    es.onerror = () => {
      if (eventSource !== es) return;
      connected.set(false);
      events.set({});
      es.close();
      eventSource = null;
      // Fetch a new SSE token on reconnect (tokens are single-use)
      scheduleReconnect(customUrl);
    };
  } finally {
    connecting = false;
  }
}

export function disconnect() {
  stopped = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  eventSource?.close();
  eventSource = null;
  connected.set(false);
  reconnectAttempts.set(0);
  events.set({});
}

export const sse = {
  connected,
  events,
  connect,
  disconnect,
  subscribe,
};
