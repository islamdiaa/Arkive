/**
 * Browser download / clipboard helpers.
 */

/** Save a Blob as a file via a temporary object URL. */
export function downloadBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	try {
		const link = document.createElement('a');
		link.href = url;
		link.download = filename;
		link.rel = 'noopener';
		document.body.appendChild(link);
		link.click();
		link.remove();
	} finally {
		URL.revokeObjectURL(url);
	}
}

/** Save plain text as a UTF-8 .txt download. */
export function downloadTextFile(text: string, filename: string): void {
	downloadBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), filename);
}

/**
 * Copy text to the clipboard. Falls back to a hidden textarea + execCommand
 * when the async Clipboard API is unavailable (plain-HTTP origins, which is
 * common for LAN-served Unraid apps).
 */
export async function copyText(text: string): Promise<boolean> {
	try {
		if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
			await navigator.clipboard.writeText(text);
			return true;
		}
	} catch {
		/* fall through to the legacy path */
	}
	// The text may be a secret (the recovery kit): the temporary textarea must
	// leave the DOM even when selecting or copying throws.
	const area = document.createElement('textarea');
	try {
		area.value = text;
		area.setAttribute('readonly', '');
		area.style.position = 'fixed';
		area.style.opacity = '0';
		document.body.appendChild(area);
		area.select();
		return document.execCommand('copy');
	} catch {
		return false;
	} finally {
		area.value = '';
		area.remove();
	}
}
