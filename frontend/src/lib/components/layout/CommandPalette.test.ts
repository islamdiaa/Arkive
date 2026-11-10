import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const goto = vi.fn();
vi.mock('$app/navigation', () => ({ goto: (...args: unknown[]) => goto(...args) }));

import CommandPalette from './CommandPalette.svelte';

describe('CommandPalette', () => {
	beforeEach(() => goto.mockReset());

	it('is a combobox controlling a listbox, with focus in the input', async () => {
		render(CommandPalette, { props: { open: true } });
		const input = screen.getByRole('combobox', { name: 'Search pages' });
		await waitFor(() => expect(input).toHaveFocus());
		const list = screen.getByRole('listbox');
		expect(input).toHaveAttribute('aria-controls', list.id);
		expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-0');
		// Options are not separate tab stops.
		expect(list.querySelectorAll('button')).toHaveLength(0);
	});

	it('moves one option per ArrowDown/ArrowUp press', async () => {
		render(CommandPalette, { props: { open: true } });
		const input = screen.getByRole('combobox');
		await fireEvent.keyDown(input, { key: 'ArrowDown' });
		expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-1');
		await fireEvent.keyDown(input, { key: 'ArrowDown' });
		expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-2');
		await fireEvent.keyDown(input, { key: 'ArrowUp' });
		expect(input).toHaveAttribute('aria-activedescendant', 'command-palette-option-1');
		expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true');
	});

	it('navigates once on Enter', async () => {
		render(CommandPalette, { props: { open: true } });
		const input = screen.getByRole('combobox');
		await fireEvent.keyDown(input, { key: 'ArrowDown' });
		await fireEvent.keyDown(input, { key: 'Enter' });
		expect(goto).toHaveBeenCalledTimes(1);
		expect(goto).toHaveBeenCalledWith('/backups');
	});

	it('dispatches close once on Escape', async () => {
		const onClose = vi.fn();
		render(CommandPalette, { props: { open: true }, events: { close: onClose } });
		await fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);
	});
});
