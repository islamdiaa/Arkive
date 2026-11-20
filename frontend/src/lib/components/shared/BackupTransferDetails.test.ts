import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import BackupTransferDetails from './BackupTransferDetails.svelte';

describe('BackupTransferDetails', () => {
  it('shows measured source progress, average speed and a per-target estimate', () => {
    render(BackupTransferDetails, { props: { transfer: {
      bytes_done: 1048576, total_bytes: 2097152, percent_done: 0.5,
      files_done: 2, total_files: 4, seconds_elapsed: 2, seconds_remaining: 65,
    }, target: 'Remote' } });
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
    expect(screen.getByText('1 MB / 2 MB (50%)')).toBeInTheDocument();
    expect(screen.getByText('512 KB/s')).toBeInTheDocument();
    expect(screen.getByText('~1m 5s')).toBeInTheDocument();
    expect(screen.getByText('Target: Remote')).toBeInTheDocument();
  });

  it('leaves unknown totals and estimates indeterminate', () => {
    render(BackupTransferDetails, { transfer: { bytes_done: 0, seconds_elapsed: 0 } });
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
    expect(screen.getByText('0 B / Scanning…')).toBeInTheDocument();
    expect(screen.getAllByText('Calculating…')).toHaveLength(2);
  });
});
