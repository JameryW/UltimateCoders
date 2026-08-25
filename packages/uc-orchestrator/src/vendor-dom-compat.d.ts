// Compat shims for checking vendored oh-my-pi sources (see tsconfig paths).
//
// bun-types omits deprecated-but-valid HTML tags from
// HTMLElementTagNameMap; upstream code (e.g. turndown GFM strikethrough
// filter) references them as plain tag-name strings. Re-add the ones the
// vendored sources use so `pnpm run check` reflects real runtime matching.
declare global {
	// eslint-disable-next-line @typescript-eslint/no-empty-interface
	interface HTMLElementTagNameMap {
		strike: HTMLElement;
	}
}

export {};
