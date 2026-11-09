import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';

import OffsiteWarningBanner from './OffsiteWarningBanner.svelte';

const localOnly = {
	has_offsite_copy: false,
	warning: true,
	uncertain: false,
	enabled_targets: 2,
	offsite_targets: 0,
	local_targets: [
		{ id: 'usb', name: 'USB Disk', type: 'local', reason: '/mnt/disks/usb is a folder on this server' },
		{ id: 'minio', name: 'MinIO', type: 's3', reason: 'minio resolves to 172.20.0.3, the container minio on this server' },
	],
	unused_offsite_targets: [] as Array<{ id: string; name: string; type: string }>,
	message: 'Every storage target the enabled backup jobs upload to is on this server, so there is no offsite copy.',
};

describe('OffsiteWarningBanner', () => {
	it('warns and links to adding a target when every target is on this server', () => {
		render(OffsiteWarningBanner, { props: { offsite: localOnly } });
		const banner = screen.getByTestId('offsite-warning');
		expect(banner).toHaveTextContent('No offsite copy');
		// A warning, not an alert: it must not block or shout over real failures.
		expect(banner).not.toHaveAttribute('role', 'alert');
		expect(screen.getByTestId('offsite-add-target')).toHaveAttribute('href', '/settings/targets?add=1');
		expect(screen.queryByTestId('offsite-review-jobs')).toBeNull();
		expect(screen.queryByTestId('offsite-unused')).toBeNull();
		expect(banner).toHaveTextContent('USB Disk — /mnt/disks/usb is a folder on this server');
		expect(banner).toHaveTextContent('MinIO — minio resolves to 172.20.0.3, the container minio on this server');
	});

	it('points to the jobs when an offsite target exists but no job uploads to it', () => {
		render(OffsiteWarningBanner, {
			props: {
				offsite: {
					...localOnly,
					local_targets: [localOnly.local_targets[0]],
					unused_offsite_targets: [{ id: 'b2', name: 'B2 Cloud', type: 'b2' }],
				},
			},
		});
		// Adding another target would not help: the job is pinned to the local one.
		expect(screen.queryByTestId('offsite-add-target')).toBeNull();
		expect(screen.getByTestId('offsite-review-jobs')).toHaveAttribute('href', '/settings/jobs');
		expect(screen.getByTestId('offsite-unused')).toHaveTextContent(
			'B2 Cloud is enabled, but no enabled backup job uploads to it.'
		);
	});

	it('is hidden when an offsite copy exists, with no targets, when unsure, or without the field', () => {
		const ok = render(OffsiteWarningBanner, {
			props: { offsite: { ...localOnly, warning: false, has_offsite_copy: true, offsite_targets: 1 } },
		});
		expect(ok.queryByTestId('offsite-warning')).toBeNull();
		ok.unmount();

		const none = render(OffsiteWarningBanner, {
			props: { offsite: { ...localOnly, warning: false, enabled_targets: 0, local_targets: [] } },
		});
		expect(none.queryByTestId('offsite-warning')).toBeNull();
		none.unmount();

		const unsure = render(OffsiteWarningBanner, {
			props: { offsite: { ...localOnly, warning: false, uncertain: true, offsite_targets: 1 } },
		});
		expect(unsure.queryByTestId('offsite-warning')).toBeNull();
		unsure.unmount();

		const missing = render(OffsiteWarningBanner, { props: { offsite: undefined } });
		expect(missing.queryByTestId('offsite-warning')).toBeNull();
	});
});
