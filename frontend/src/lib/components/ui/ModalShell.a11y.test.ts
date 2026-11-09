import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ConfirmDangerModal from './ConfirmDangerModal.svelte';
import ModalShell from './ModalShell.svelte';

// ConfirmDangerModal is the ModalShell consumer with real content (a text
// field and footer buttons), so it exercises focus handling end to end.

describe('ModalShell keyboard and focus handling', () => {
	it('moves focus to the first form field when it opens', async () => {
		render(ConfirmDangerModal, { props: { open: true, title: 'Delete thing' } });
		await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus());
	});

	it('returns focus to the element that opened it when it closes', async () => {
		const opener = document.createElement('button');
		opener.textContent = 'Open';
		document.body.appendChild(opener);
		opener.focus();

		const { rerender } = render(ConfirmDangerModal, { props: { open: false, title: 'Delete thing' } });
		await rerender({ open: true });
		await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus());

		await rerender({ open: false });
		await tick();
		expect(opener).toHaveFocus();
		opener.remove();
	});

	it('closes on Escape while focus is inside the dialog', async () => {
		render(ConfirmDangerModal, { props: { open: true, title: 'Delete thing' } });
		const field = screen.getByRole('textbox');
		await waitFor(() => expect(field).toHaveFocus());
		// Dispatched on the focused field so it has to bubble out of the panel.
		await fireEvent.keyDown(field, { key: 'Escape' });
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
	});

	it('wraps Tab from the last control to the first, and Shift+Tab back', async () => {
		render(ConfirmDangerModal, { props: { open: true, title: 'Delete thing' } });
		const dialog = screen.getByRole('dialog');
		const close = screen.getByRole('button', { name: 'Close modal' });
		const cancel = screen.getByRole('button', { name: 'Cancel' });
		await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus());
		// The confirm button is disabled until the text matches, so Cancel is last.
		cancel.focus();
		await fireEvent.keyDown(cancel, { key: 'Tab' });
		expect(close).toHaveFocus();
		await fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
		expect(cancel).toHaveFocus();
		expect(dialog.contains(document.activeElement)).toBe(true);
	});

	it('pulls focus back in if it somehow escaped the dialog', async () => {
		const outside = document.createElement('button');
		document.body.appendChild(outside);
		render(ConfirmDangerModal, { props: { open: true, title: 'Delete thing' } });
		await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus());
		outside.focus();
		await fireEvent.keyDown(outside, { key: 'Tab' });
		expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true);
		outside.remove();
	});

	it('closes when the backdrop area is clicked but not when the panel is', async () => {
		const { container } = render(ModalShell, { props: { open: true, title: 'Test' } });
		const dialog = screen.getByRole('dialog');
		await fireEvent.click(dialog);
		expect(screen.queryByRole('dialog')).toBeTruthy();

		const layer = dialog.parentElement as HTMLElement;
		await fireEvent.click(layer);
		await waitFor(() => expect(container.querySelector('[role="dialog"]')).toBeNull());
	});

	it('stays open on backdrop click when closeOnBackdrop is false', async () => {
		render(ModalShell, { props: { open: true, title: 'Test', closeOnBackdrop: false } });
		const layer = screen.getByRole('dialog').parentElement as HTMLElement;
		await fireEvent.click(layer);
		expect(screen.queryByRole('dialog')).toBeTruthy();
	});
});
