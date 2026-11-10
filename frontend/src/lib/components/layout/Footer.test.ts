import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import Footer from './Footer.svelte';

describe('Footer', () => {
	it('links to the author site, GitHub profile and source', () => {
		render(Footer);
		expect(screen.getByRole('link', { name: /itayar\.com/ })).toHaveAttribute('href', 'https://itayar.com');
		expect(screen.getByRole('link', { name: /GitHub/ })).toHaveAttribute('href', 'https://github.com/islamdiaa');
		expect(screen.getByRole('link', { name: /Source/ })).toHaveAttribute(
			'href',
			'https://github.com/islamdiaa/Arkive'
		);
		for (const link of screen.getAllByRole('link')) {
			expect(link).toHaveAttribute('rel', 'noopener noreferrer');
		}
	});
});
