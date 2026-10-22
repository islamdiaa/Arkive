import { describe, expect, it } from 'vitest';

import { describeCron } from './schedule';

function at(hour: number, minute: number): string {
	const date = new Date();
	date.setHours(hour, minute, 0, 0);
	return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

describe('describeCron', () => {
	it('keeps the existing presets', () => {
		expect(describeCron('0 3 * * 0')).toBe('Weekly on Sunday at 3:00 AM');
		expect(describeCron('0 */6 * * *')).toBe('Every 6 hours');
		expect(describeCron('  0   6,18 * * * ')).toBe('Twice daily at 6:00 AM and 6:00 PM');
	});

	it('describes weekly schedules such as the default restore drill', () => {
		expect(describeCron('0 7 * * 0')).toBe(`Weekly on Sunday at ${at(7, 0)}`);
		expect(describeCron('30 22 * * 6')).toBe(`Weekly on Saturday at ${at(22, 30)}`);
		expect(describeCron('0 4 * * 7')).toBe(`Weekly on Sunday at ${at(4, 0)}`);
	});

	it('describes daily schedules and leaves complex expressions as-is', () => {
		expect(describeCron('15 1 * * *')).toBe(`Daily at ${at(1, 15)}`);
		expect(describeCron('0 7 * * 1-5')).toBe('0 7 * * 1-5');
		expect(describeCron('0 99 * * 0')).toBe('0 99 * * 0');
	});
});
