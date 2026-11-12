import { describe, expect, it } from 'vitest';
import { settingsSchema } from './settings';

describe('backup timeout setting', () => {
  it.each([0, 60, 1440, 10080])('accepts %s minutes', (minutes) => {
    expect(settingsSchema.safeParse({ backup_timeout_minutes: minutes }).success).toBe(true);
  });

  it.each([-1, 0.5, 10081, NaN])('rejects %s minutes', (minutes) => {
    expect(settingsSchema.safeParse({ backup_timeout_minutes: minutes }).success).toBe(false);
  });
});
