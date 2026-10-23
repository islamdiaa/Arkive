import { api } from './client';
import type { ArkiveSettings, HeartbeatTestResult, RecoveryKit } from '$lib/types';

export async function getSettings(): Promise<ArkiveSettings> {
  return api.get('/settings');
}

export async function updateSettings(data: Partial<ArkiveSettings>): Promise<ArkiveSettings> {
  return api.put('/settings', data);
}

export type SettingInput = string | number | boolean | null | undefined;

/**
 * Body for the bulk PUT /api/settings endpoint: `{settings: {key: "value"}}`.
 * The backend stores every setting as a string; booleans become "true"/"false"
 * and null/undefined become "".
 */
export function toSettingsPayload(values: Record<string, SettingInput>): { settings: Record<string, string> } {
  const settings: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value === null || value === undefined) settings[key] = '';
    else if (typeof value === 'boolean') settings[key] = value ? 'true' : 'false';
    else settings[key] = String(value).trim();
  }
  return { settings };
}

/** Bulk-save settings (string values). */
export async function saveSettings(values: Record<string, SettingInput>): Promise<unknown> {
  return api.put('/settings', toSettingsPayload(values));
}

/** Generate a recovery kit. The response contains secrets: never persist it. */
export async function generateRecoveryKit(): Promise<RecoveryKit> {
  return api.post('/settings/recovery-kit');
}

export async function confirmRecoveryKit(): Promise<{ confirmed_at: string }> {
  return api.post('/settings/recovery-kit/confirm');
}

/** Ping a heartbeat URL now: the given (possibly unsaved) one, or the saved setting. */
export async function testHeartbeat(url?: string): Promise<HeartbeatTestResult> {
  const trimmed = url?.trim();
  return api.post('/settings/heartbeat/test', trimmed ? { url: trimmed } : undefined);
}

/** One-line summary of a heartbeat test result. */
export function describeHeartbeatResult(result: HeartbeatTestResult): string {
  const code = typeof result.status_code === 'number' ? `HTTP ${result.status_code}` : '';
  if (result.ok) return `Ping delivered${code ? ` (${code})` : ''}. Your monitor should show a heartbeat now.`;
  const reason = [code, result.error].filter(Boolean).join(': ');
  return `Ping failed${reason ? ` — ${reason}` : ''}`;
}

/** "" or an http(s) URL, mirroring the backend's heartbeat_url validation. */
export function isValidHeartbeatUrl(value: string): boolean {
  const text = value.trim();
  if (!text) return true;
  if (text.length > 2048) return false;
  return /^https?:\/\/[^\s/?#]+\S*$/i.test(text);
}

/** Integer within [min, max] (accepts numeric strings). */
export function isIntInRange(value: unknown, min: number, max: number): boolean {
  if (value === '' || value === null || value === undefined) return false;
  const n = Number(value);
  return Number.isInteger(n) && n >= min && n <= max;
}
