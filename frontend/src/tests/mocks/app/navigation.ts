// Test stand-in for SvelteKit's $app/navigation (aliased in vitest.config.ts).
// Tests that assert on navigation replace these with vi.mock().
export const goto = async (_url: string | URL, _opts?: unknown): Promise<void> => {};
export const invalidate = async (_url: string | URL): Promise<void> => {};
export const invalidateAll = async (): Promise<void> => {};
export const beforeNavigate = (_cb: unknown): void => {};
export const afterNavigate = (_cb: unknown): void => {};
export const onNavigate = (_cb: unknown): void => {};
