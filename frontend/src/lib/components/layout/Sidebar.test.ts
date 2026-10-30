import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';

// We need to mock $app/stores with a writable page store so we can change routes
// The mock at src/tests/mocks/app/stores.ts provides the default,
// but the Sidebar component imports from $app/stores which is aliased in vitest config.

import Sidebar from './Sidebar.svelte';
import { systemStatus } from '$lib/stores/app';

describe('Sidebar', () => {
	beforeEach(() => {
		// Reset DOM between tests
	});

	it('renders the Arkive branding', () => {
		render(Sidebar);
		expect(screen.getByText('Arkive')).toBeInTheDocument();
		expect(screen.getByText('Disaster Recovery')).toBeInTheDocument();
	});

	it('renders the logo letter', () => {
		render(Sidebar);
		expect(screen.getByText('A')).toBeInTheDocument();
	});

	it('renders all main navigation links', () => {
		render(Sidebar);
		expect(screen.getByText('Dashboard')).toBeInTheDocument();
		expect(screen.getByText('Backups')).toBeInTheDocument();
		expect(screen.getByText('Snapshots')).toBeInTheDocument();
		expect(screen.getByText('Databases')).toBeInTheDocument();
		expect(screen.getByText('Activity')).toBeInTheDocument();
		expect(screen.getByText('Logs')).toBeInTheDocument();
		expect(screen.getByText('Jobs')).toBeInTheDocument();
		expect(screen.getByText('Settings')).toBeInTheDocument();
	});

	it('renders the Settings nav item', () => {
		render(Sidebar);
		expect(screen.getByText('Settings')).toBeInTheDocument();
	});

	it('renders navigation links with correct href attributes', () => {
		render(Sidebar);
		const dashboardLink = screen.getByText('Dashboard').closest('a');
		expect(dashboardLink).toHaveAttribute('href', '/');

		const backupsLink = screen.getByText('Backups').closest('a');
		expect(backupsLink).toHaveAttribute('href', '/backups');

		const snapshotsLink = screen.getByText('Snapshots').closest('a');
		expect(snapshotsLink).toHaveAttribute('href', '/snapshots');

		const databasesLink = screen.getByText('Databases').closest('a');
		expect(databasesLink).toHaveAttribute('href', '/databases');

		const activityLink = screen.getByText('Activity').closest('a');
		expect(activityLink).toHaveAttribute('href', '/activity');
	});

	it('renders the server version from /api/status', () => {
		systemStatus.set({ version: '0.4.0' });
		render(Sidebar);
		expect(screen.getByText('v0.4.0')).toBeInTheDocument();
		systemStatus.set(null);
	});

	it('renders no version before status has loaded', () => {
		systemStatus.set(null);
		render(Sidebar);
		expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument();
	});

	it('renders Community badge', () => {
		render(Sidebar);
		expect(screen.getByText('Community')).toBeInTheDocument();
	});

	it('highlights active route for Dashboard (default)', () => {
		render(Sidebar);
		const dashboardLink = screen.getByText('Dashboard').closest('a');
		// The default mock page URL is http://localhost (pathname = '/')
		// so Dashboard should have the active class
		expect(dashboardLink?.className).toContain('text-primary');
	});
});
