import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';

import OffsiteWarningBanner from './OffsiteWarningBanner.svelte';

const localOnly = {
	has_offsite_copy: false,
	warning: true,
	enabled_targets: 2,
	offsite_targets: 0,
	local_targets: [
		{ id: 'usb', name: 'USB Disk', type: 'local', reason: '/mnt/disks/usb is a folder on this server' },
		{ id: 'minio', name: 'MinIO', type: 's3', reason: 'minio is a container on this server' },
	],
	message: 'Every enabled storage target is on this server, so there is no offsite copy.',
};

describe('OffsiteWarningBanner', () => {
	it('warns and links to adding a target when every target is on this server', () => {
		render(OffsiteWarningBanner, { props: { offsite: localOnly } });
		const banner = screen.getByTestId('offsite-warning');
		expect(banner).toHaveTextContent('No offsite copy');
		// A warning, not an alert: it must not block or shout over real failures.
		expect(banner).not.toHaveAttribute('role', 'alert');
		expect(screen.getByTestId('offsite-add-target')).toHaveAttribute('href', '/settings/targets?add=1');
		expect(banner).toHaveTextContent('USB Disk — /mnt/disks/usb is a folder on this server');
		expect(banner).toHaveTextContent('MinIO — minio is a container on this server');
	});

	it('is hidden when an offsite copy exists, with no targets, or without the field', () => {
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

		const missing = render(OffsiteWarningBanner, { props: { offsite: undefined } });
		expect(missing.queryByTestId('offsite-warning')).toBeNull();
	});
});
