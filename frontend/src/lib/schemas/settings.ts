import { z } from 'zod';

const cronExpression = z
  .string()
  .trim()
  .refine((v) => v.split(/\s+/).length === 5, 'Use a 5-field cron expression (minute hour day month weekday)');

export const settingsSchema = z.object({
  server_name: z.string().max(100).optional(),
  timezone: z.string().optional(),
  retention_days: z.number().int().min(1).max(365).optional(),
  keep_daily: z.number().int().min(1).max(365).optional(),
  keep_weekly: z.number().int().min(0).max(52).optional(),
  keep_monthly: z.number().int().min(0).max(24).optional(),
  log_level: z.enum(['DEBUG', 'INFO', 'WARN', 'ERROR']).optional(),
  dump_compression: z.enum(['none', 'gzip']).optional(),
  restore_drill_enabled: z.boolean().optional(),
  restore_drill_schedule: cronExpression.optional(),
  restore_drill_max_databases: z.number().int('Must be a whole number').min(1).max(100).optional(),
  restore_drill_max_dump_mb: z.number().int('Must be a whole number').min(1).max(1048576).optional(),
});
