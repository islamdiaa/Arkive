import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';

import StaleBackupsAlert from './StaleBackupsAlert.svelte';
import RecoveryKitBanner from './RecoveryKitBanner.svelte';

const staleFreshness = {
	stale: true,
	checked_at: new Date().toISOString(),
	stale_jobs: [
		{
			job_id: 'a1',
			job_name: 'DB Dumps',
			last_good_at: new Date(Date.now() - 2 * 86400000 - 60000).toISOString(),
			threshold_hours: 15,
			overdue_hours: 33,
		},
		{ job_id: 'b2', job_name: 'Full Backup', last_good_at: null, threshold_hours: 30, overdue_hours: 2 },
	],
};

describe('StaleBackupsAlert', () => {
	it('renders nothing when backups are fresh', () => {
		const { container } = render(StaleBackupsAlert, {
			props: { freshness: { stale: false, checked_at: null, stale_jobs: [] } },
		});
		expect(container.querySelector('[role="alert"]')).toBeNull();
	});

	it('lists each stale job with a link to its run history', () => {
		render(StaleBackupsAlert, { props: { freshness: staleFreshness } });
		expect(screen.getByRole('alert')).toBeInTheDocument();
		expect(screen.getByText('2 backup jobs are overdue')).toBeInTheDocument();

		const dbLine = screen.getByText('DB Dumps — last good backup 2 days ago (expected within 15 h)');
		expect(dbLine.closest('a')).toHaveAttribute('href', '/backups?job=a1');

		const neverLine = screen.getByText('Full Backup — last good backup never (expected within 30 h)');
		expect(neverLine.closest('a')).toHaveAttribute('href', '/backups?job=b2');
	});

	it('still alerts when the job list is empty', () => {
		render(StaleBackupsAlert, { props: { freshness: { stale: true, checked_at: null, stale_jobs: [] } } });
		expect(screen.getByText('Backups are overdue')).toBeInTheDocument();
	});
});

describe('RecoveryKitBanner', () => {
	it('links to the recovery kit page when unconfirmed', () => {
		render(RecoveryKitBanner, { props: { confirmed: false } });
		const banner = screen.getByTestId('recovery-kit-banner');
		expect(banner).toHaveAttribute('href', '/settings/recovery-kit');
		expect(banner.textContent).toContain(
			"Save your recovery kit — without the encryption password stored somewhere other than this server, your backups can't be restored if this server dies.",
		);
	});

	it('is hidden once confirmed or when the field is missing', () => {
		const confirmed = render(RecoveryKitBanner, { props: { confirmed: true } });
		expect(confirmed.queryByTestId('recovery-kit-banner')).toBeNull();
		confirmed.unmount();

		const missing = render(RecoveryKitBanner, { props: { confirmed: undefined } });
		expect(missing.queryByTestId('recovery-kit-banner')).toBeNull();
	});
});
