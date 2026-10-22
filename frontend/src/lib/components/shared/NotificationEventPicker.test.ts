import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';

import NotificationEventPicker from './NotificationEventPicker.svelte';
import { fallbackEventCatalog } from '$lib/utils/notification-events';

describe('NotificationEventPicker', () => {
	it('renders every catalog event with its label and marks selected ones', () => {
		const catalog = fallbackEventCatalog();
		render(NotificationEventPicker, { props: { catalog, selected: ['backup.failed'] } });
		for (const event of catalog.events) {
			expect(screen.getByText(event.label)).toBeInTheDocument();
		}
		expect(screen.getByText('Backup failed').closest('button')).toHaveAttribute('aria-pressed', 'true');
		expect(screen.getByText('Backups overdue').closest('button')).toHaveAttribute('aria-pressed', 'false');
		expect(screen.queryByText('backup.started')).toBeNull();
	});

	it('toggles a selection on click', async () => {
		render(NotificationEventPicker, { props: { catalog: fallbackEventCatalog(), selected: [] } });
		const button = screen.getByText('Restore drill failed').closest('button')!;
		await fireEvent.click(button);
		expect(button).toHaveAttribute('aria-pressed', 'true');
		await fireEvent.click(button);
		expect(button).toHaveAttribute('aria-pressed', 'false');
	});

	it('lists legacy events that will be dropped on save', () => {
		render(NotificationEventPicker, {
			props: { catalog: fallbackEventCatalog(), selected: ['backup.failed'], dropped: ['backup.started', 'system.warning'] },
		});
		const notice = screen.getByTestId('dropped-events');
		expect(notice.textContent).toContain('backup.started, system.warning');
		expect(notice.textContent).toContain('dropped when you save');
	});

	it('notes when the built-in fallback list is shown', () => {
		render(NotificationEventPicker, { props: { catalog: fallbackEventCatalog(), fromFallback: true } });
		expect(screen.getByText(/showing the built-in list/)).toBeInTheDocument();
	});
});
