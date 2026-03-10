import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import StatusBadge from './StatusBadge.svelte';

describe('StatusBadge', () => {
	it('renders status text', () => {
		render(StatusBadge, { props: { status: 'success' } });
		expect(screen.getByText('success')).toBeTruthy();
	});

	it('applies success CSS variable classes', () => {
		const { container } = render(StatusBadge, { props: { status: 'success' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-success-bg');
		expect(badge?.className).toContain('text-success');
	});

	it('applies danger classes for failed status', () => {
		const { container } = render(StatusBadge, { props: { status: 'failed' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-danger-bg');
		expect(badge?.className).toContain('text-danger');
	});

	it('applies danger classes for error status', () => {
		const { container } = render(StatusBadge, { props: { status: 'error' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-danger-bg');
		expect(badge?.className).toContain('text-danger');
	});

	it('applies info classes for running status with pulse animation', () => {
		const { container } = render(StatusBadge, { props: { status: 'running' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-info-bg');
		expect(badge?.className).toContain('text-info');
		expect(badge?.className).toContain('animate-status-pulse');
	});

	it('applies info classes for in_progress status with pulse animation', () => {
		const { container } = render(StatusBadge, { props: { status: 'in_progress' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-info-bg');
		expect(badge?.className).toContain('animate-status-pulse');
	});

	it('applies warning classes for partial status', () => {
		const { container } = render(StatusBadge, { props: { status: 'partial' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-warning-bg');
		expect(badge?.className).toContain('text-warning');
	});

	it('applies neutral classes for cancelled status', () => {
		const { container } = render(StatusBadge, { props: { status: 'cancelled' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-neutral-bg');
		expect(badge?.className).toContain('text-neutral');
	});

	it('falls back to neutral for unknown status', () => {
		const { container } = render(StatusBadge, { props: { status: 'foobar' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-neutral-bg');
		expect(badge?.className).toContain('text-neutral');
	});

	it('renders with small size classes', () => {
		const { container } = render(StatusBadge, { props: { status: 'success', size: 'sm' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('text-[10px]');
		expect(badge?.className).toContain('px-1.5');
	});

	it('renders with medium size by default', () => {
		const { container } = render(StatusBadge, { props: { status: 'success' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('px-2');
		expect(badge?.className).toContain('text-xs');
	});

	it('accepts extra class prop', () => {
		const { container } = render(StatusBadge, { props: { status: 'success', class: 'my-custom-class' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('my-custom-class');
	});

	it('supports completed as alias for success', () => {
		const { container } = render(StatusBadge, { props: { status: 'completed' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-success-bg');
	});

	it('supports queued status', () => {
		const { container } = render(StatusBadge, { props: { status: 'queued' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-primary-bg');
	});

	it('renders backup activity type without unknown fallback styling', () => {
		const { container } = render(StatusBadge, { props: { status: 'backup', size: 'sm' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-primary-bg');
		expect(badge?.textContent).toContain('backup');
		expect(badge?.textContent).toContain('B');
	});

	it('renders system activity type without question-mark fallback', () => {
		const { container } = render(StatusBadge, { props: { status: 'system', size: 'sm' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('bg-neutral-bg');
		expect(badge?.textContent).toContain('system');
		expect(badge?.textContent).toContain('S');
	});

	it('has pill shape (rounded-full)', () => {
		const { container } = render(StatusBadge, { props: { status: 'success' } });
		const badge = container.querySelector('span');
		expect(badge?.className).toContain('rounded-full');
	});
});
