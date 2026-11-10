import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import FormToggle from './FormToggle.svelte';

describe('FormToggle', () => {
	it('exposes a switch named by its visible label even without an id', () => {
		render(FormToggle, { props: { label: 'Scheduled drills', checked: true } });
		const toggle = screen.getByRole('switch', { name: 'Scheduled drills' });
		expect(toggle).toHaveAttribute('aria-checked', 'true');
		// The <label> points at the switch (a generated id, not for="").
		expect(screen.getByText('Scheduled drills')).toHaveAttribute('for', toggle.id);
		expect(toggle.id).not.toBe('');
	});

	it('keeps a caller-supplied id', () => {
		render(FormToggle, { props: { id: 'drill-toggle', label: 'Drills' } });
		expect(screen.getByRole('switch')).toHaveAttribute('id', 'drill-toggle');
	});

	it('uses ariaLabel when there is no visible label', () => {
		render(FormToggle, { props: { ariaLabel: 'Enable Nightly', checked: false } });
		const toggle = screen.getByRole('switch', { name: 'Enable Nightly' });
		expect(toggle).toHaveAttribute('aria-checked', 'false');
	});

	it('describes the switch with its description text', () => {
		render(FormToggle, { props: { label: 'Drills', description: 'Runs weekly' } });
		expect(screen.getByRole('switch')).toHaveAccessibleDescription('Runs weekly');
	});

	it('generates distinct ids for separate instances', () => {
		render(FormToggle, { props: { ariaLabel: 'One' } });
		render(FormToggle, { props: { ariaLabel: 'Two' } });
		const [a, b] = screen.getAllByRole('switch');
		expect(a.id).not.toBe(b.id);
	});

	it('dispatches change with the next state on click', async () => {
		let detail: { checked: boolean } | undefined;
		render(FormToggle, {
			props: { ariaLabel: 'Toggle', checked: false },
			events: { change: (e: CustomEvent<{ checked: boolean }>) => (detail = e.detail) },
		});
		await fireEvent.click(screen.getByRole('switch'));
		expect(detail).toEqual({ checked: true });
	});
});
