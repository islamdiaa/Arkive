import { describe, expect, it } from 'vitest';

import { describeHeartbeatResult, isIntInRange, isValidHeartbeatUrl, toSettingsPayload } from './settings';

describe('toSettingsPayload', () => {
	it('wraps values in {settings} and stringifies them for the bulk endpoint', () => {
		expect(
			toSettingsPayload({
				heartbeat_url: '  https://uptime.example.com/api/push/abc  ',
				stale_backup_hours: 0,
				restore_drill_enabled: false,
				restore_drill_max_databases: 3,
				dump_compression: 'none',
			}),
		).toEqual({
			settings: {
				heartbeat_url: 'https://uptime.example.com/api/push/abc',
				stale_backup_hours: '0',
				restore_drill_enabled: 'false',
				restore_drill_max_databases: '3',
				dump_compression: 'none',
			},
		});
	});

	it('maps true and empty values', () => {
		expect(toSettingsPayload({ a: true, b: null, c: undefined, d: '' })).toEqual({
			settings: { a: 'true', b: '', c: '', d: '' },
		});
	});
});

describe('isValidHeartbeatUrl', () => {
	it('accepts empty (disabled) and http(s) URLs', () => {
		expect(isValidHeartbeatUrl('')).toBe(true);
		expect(isValidHeartbeatUrl('   ')).toBe(true);
		expect(isValidHeartbeatUrl('https://hc-ping.com/1234-uuid')).toBe(true);
		expect(isValidHeartbeatUrl('http://192.168.1.100:3001/api/push/abc?status=up')).toBe(true);
	});

	it('rejects other schemes and malformed input', () => {
		expect(isValidHeartbeatUrl('ftp://example.com')).toBe(false);
		expect(isValidHeartbeatUrl('uptime.example.com/push')).toBe(false);
		expect(isValidHeartbeatUrl('https://')).toBe(false);
		expect(isValidHeartbeatUrl('https://exa mple.com')).toBe(false);
		expect(isValidHeartbeatUrl(`https://example.com/${'a'.repeat(2048)}`)).toBe(false);
	});
});

describe('isIntInRange', () => {
	it('checks integer bounds', () => {
		expect(isIntInRange(0, 0, 8760)).toBe(true);
		expect(isIntInRange('24', 0, 8760)).toBe(true);
		expect(isIntInRange(8760, 0, 8760)).toBe(true);
		expect(isIntInRange(8761, 0, 8760)).toBe(false);
		expect(isIntInRange(-1, 0, 8760)).toBe(false);
		expect(isIntInRange(1.5, 0, 8760)).toBe(false);
		expect(isIntInRange('', 0, 8760)).toBe(false);
		expect(isIntInRange(null, 0, 8760)).toBe(false);
		expect(isIntInRange(undefined, 0, 8760)).toBe(false);
	});
});

describe('describeHeartbeatResult', () => {
	it('describes success with the status code', () => {
		expect(describeHeartbeatResult({ ok: true, status_code: 200, error: null })).toBe(
			'Ping delivered (HTTP 200). Your monitor should show a heartbeat now.',
		);
	});

	it('describes failures with status code and error', () => {
		expect(describeHeartbeatResult({ ok: false, status_code: 404, error: 'Not Found' })).toBe(
			'Ping failed — HTTP 404: Not Found',
		);
		expect(describeHeartbeatResult({ ok: false, status_code: null, error: 'Connection refused' })).toBe(
			'Ping failed — Connection refused',
		);
		expect(describeHeartbeatResult({ ok: false, status_code: null, error: null })).toBe('Ping failed');
	});
});
