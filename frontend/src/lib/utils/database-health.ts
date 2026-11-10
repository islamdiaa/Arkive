/**
 * Display helpers for per-database backup health (GET /databases/health).
 */
import type { DatabaseDrillResult, DatabaseHealth, DatabaseSizePoint } from '$lib/types';

export interface HealthStatusStyle {
	label: string;
	badge: string;
}

const STATUS_STYLES: Record<string, HealthStatusStyle> = {
	ok: { label: 'Healthy', badge: 'bg-success-bg text-success' },
	warning: { label: 'Needs attention', badge: 'bg-warning-bg text-warning' },
	failed: { label: 'Dump failing', badge: 'bg-danger-bg text-danger' },
	never_dumped: { label: 'Not backed up yet', badge: 'bg-danger-bg text-danger' },
	excluded: { label: 'Excluded', badge: 'bg-neutral-bg text-neutral' },
};

export function healthStatusStyle(status: string | null | undefined): HealthStatusStyle {
	return STATUS_STYLES[status ?? ''] ?? { label: status || 'Unknown', badge: 'bg-neutral-bg text-neutral' };
}

/** True when a database needs the user's attention. */
export function needsAttention(item: Pick<DatabaseHealth, 'status'>): boolean {
	return item.status === 'warning' || item.status === 'failed' || item.status === 'never_dumped';
}

/** Health items indexed by database key, for joining with the database list. */
export function indexHealth(items: DatabaseHealth[] | null | undefined): Map<string, DatabaseHealth> {
	const map = new Map<string, DatabaseHealth>();
	for (const item of items ?? []) {
		if (item?.key) map.set(item.key, item);
	}
	return map;
}

/** "+1.2%", "−62%", "±0%"; '' when unknown. */
export function formatSizeChange(percent: number | null | undefined): string {
	if (typeof percent !== 'number' || !Number.isFinite(percent)) return '';
	if (Math.abs(percent) < 0.05) return '±0%';
	const magnitude = Math.abs(percent) >= 10 ? Math.round(Math.abs(percent)) : Math.round(Math.abs(percent) * 10) / 10;
	return `${percent > 0 ? '+' : '−'}${magnitude}%`;
}

export interface DrillSummary {
	label: string;
	tone: 'success' | 'danger' | 'muted';
	title: string;
}

export function drillSummary(drill: DatabaseDrillResult | null | undefined): DrillSummary {
	if (!drill) return { label: 'Never drilled', tone: 'muted', title: 'No restore drill has tested this database yet' };
	const detail = drill.detail || '';
	switch (drill.status) {
		case 'passed':
			return { label: 'Passed', tone: 'success', title: detail || 'The dump restored into a fresh instance' };
		case 'failed':
			return { label: 'Failed', tone: 'danger', title: detail || 'The dump did not restore' };
		default:
			return { label: 'Not tested', tone: 'muted', title: detail || 'Skipped by the last drill' };
	}
}

/** Smallest vertical range of the size trend, as a fraction of the largest dump. */
export const MIN_RELATIVE_SPAN = 0.2;

export interface TrendDot {
	x: number;
	y: number;
	status: string;
	shrunk: boolean;
	size: number | null;
	at: string;
}

export interface TrendGeometry {
	/** SVG polyline "x,y x,y" through successful dumps ('' with fewer than 2). */
	line: string;
	dots: TrendDot[];
}

/**
 * Lay a size trend (oldest first) out in a width×height box. Successful
 * dumps are scaled between the smallest and largest size, over a range of at
 * least MIN_RELATIVE_SPAN of the largest so a few percent of normal churn
 * stays visually flat; failed dumps sit on the bottom edge. Points are evenly
 * spaced in run order.
 */
export function trendGeometry(points: DatabaseSizePoint[] | null | undefined, width: number, height: number, pad = 3): TrendGeometry {
	const list = Array.isArray(points) ? points : [];
	if (list.length === 0) return { line: '', dots: [] };
	const isSized = (p: DatabaseSizePoint) => p.status === 'success' && isSize(p.size_bytes);
	const sizes = list.filter(isSized).map((p) => p.size_bytes as number);
	const max = sizes.length ? Math.max(...sizes) : 0;
	const min = sizes.length ? Math.min(Math.min(...sizes), max * (1 - MIN_RELATIVE_SPAN)) : 0;
	const innerW = Math.max(width - pad * 2, 1);
	const innerH = Math.max(height - pad * 2, 1);
	const step = list.length > 1 ? innerW / (list.length - 1) : 0;
	const round = (n: number) => Math.round(n * 10) / 10;

	const dots: TrendDot[] = list.map((p, i) => {
		const x = list.length > 1 ? pad + step * i : pad + innerW / 2;
		let y = height - pad;
		if (isSized(p)) {
			const size = p.size_bytes as number;
			y = max === min ? pad + innerH / 2 : pad + innerH - ((size - min) / (max - min)) * innerH;
		}
		return { x: round(x), y: round(y), status: p.status, shrunk: !!p.shrunk, size: p.size_bytes, at: p.at };
	});
	const sized = dots.filter((d, i) => isSized(list[i]));
	return { line: sized.length >= 2 ? sized.map((d) => `${d.x},${d.y}`).join(' ') : '', dots };
}

function isSize(value: unknown): value is number {
	return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}
