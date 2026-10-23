import { afterEach, describe, expect, it, vi } from 'vitest';

import { copyText, downloadTextFile } from './download';

describe('downloadTextFile', () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it('saves the text as a named plain-text Blob and revokes the URL', async () => {
		const blobs: Blob[] = [];
		const createObjectURL = vi.fn((blob: Blob) => {
			blobs.push(blob);
			return 'blob:mock-url';
		});
		const revokeObjectURL = vi.fn();
		Object.assign(URL, { createObjectURL, revokeObjectURL });
		let clicked: HTMLAnchorElement | null = null;
		vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
			clicked = this;
		});

		downloadTextFile('ARKIVE RECOVERY KIT\nline 2', 'arkive-recovery-kit.txt');

		expect(createObjectURL).toHaveBeenCalledTimes(1);
		expect(blobs[0].type).toBe('text/plain;charset=utf-8');
		expect(await blobs[0].text()).toBe('ARKIVE RECOVERY KIT\nline 2');
		expect(clicked).not.toBeNull();
		expect(clicked!.download).toBe('arkive-recovery-kit.txt');
		expect(clicked!.getAttribute('href')).toBe('blob:mock-url');
		expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
		// The temporary link must not be left in the DOM.
		expect(document.querySelector('a[download]')).toBeNull();
	});
});

describe('copyText', () => {
	afterEach(() => {
		vi.restoreAllMocks();
		vi.unstubAllGlobals();
	});

	it('falls back to execCommand when the Clipboard API is unavailable', async () => {
		const exec = vi.fn(() => true);
		Object.assign(document, { execCommand: exec });
		expect(await copyText('secret')).toBe(true);
		expect(exec).toHaveBeenCalledWith('copy');
		expect(document.querySelector('textarea')).toBeNull();
	});

	it('removes the secret from the DOM when the legacy copy throws', async () => {
		Object.assign(document, { execCommand: vi.fn(() => { throw new Error('unsupported'); }) });
		expect(await copyText('secret-password')).toBe(false);
		expect(document.querySelector('textarea')).toBeNull();
		expect(document.body.innerHTML).not.toContain('secret-password');
	});
});
