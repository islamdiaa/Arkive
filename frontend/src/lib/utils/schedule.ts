const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export function describeCron(expr: string): string {
  const normalized = expr.trim().replace(/\s+/g, ' ');

  const presets: Record<string, string> = {
    '0 0 * * *': 'Daily at 12:00 AM',
    '0 2 * * *': 'Daily at 2:00 AM',
    '0 3 * * *': 'Daily at 3:00 AM',
    '0 6 * * *': 'Daily at 6:00 AM',
    '0 7 * * *': 'Daily at 7:00 AM',
    '0 */6 * * *': 'Every 6 hours',
    '0 */12 * * *': 'Every 12 hours',
    '0 6,18 * * *': 'Twice daily at 6:00 AM and 6:00 PM',
    '0 3 * * 0': 'Weekly on Sunday at 3:00 AM',
  };

  if (presets[normalized]) return presets[normalized];

  const everyHours = normalized.match(/^(\d+)\s+\*\/(\d+)\s+\*\s+\*\s+\*$/);
  if (everyHours) {
    return `Every ${everyHours[2]} hours at minute ${everyHours[1]}`;
  }

  const weeklyAt = normalized.match(/^(\d+)\s+(\d+)\s+\*\s+\*\s+([0-7])$/);
  if (weeklyAt) {
    const minute = Number(weeklyAt[1]);
    const hour = Number(weeklyAt[2]);
    if (minute < 60 && hour < 24) {
      const day = WEEKDAYS[Number(weeklyAt[3]) % 7];
      const date = new Date();
      date.setHours(hour, minute, 0, 0);
      return `Weekly on ${day} at ${date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
    }
  }

  const dailyAt = normalized.match(/^(\d+)\s+(\d+)\s+\*\s+\*\s+\*$/);
  if (dailyAt) {
    const minute = Number(dailyAt[1]);
    const hour = Number(dailyAt[2]);
    if (!Number.isNaN(minute) && !Number.isNaN(hour)) {
      const date = new Date();
      date.setHours(hour, minute, 0, 0);
      return `Daily at ${date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
    }
  }

  return normalized;
}
