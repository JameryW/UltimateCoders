/**
 * Height-adaptive page size for UC overlays.
 *
 * ponytail: overlays mount via showOverlay({ maxHeight: "100%" }), so the
 * framework clamps rendered lines to terminal.rows. A hardcoded page size
 * (previously 20) overflowed short terminals (24-row), and the clamp then
 * silently cut the footer + bottom cursor rows - PgDn / cursor-down moved
 * onto clipped rows the user could not see. Size to terminal.rows minus a
 * chrome budget instead: OMP status/input/borders + overlay
 * header/hint/blank/footer ≈ 12 rows, matching OMP's own HookSelector
 * formula (Math.max(4, Math.min(15, rows - 12))).
 *
 * `tui` is the ExtensionUIContext at runtime (exposes `.terminal.rows`); it is
 * `undefined` in selfcheck / headless, where FALLBACK_VISIBLE_ROWS is returned
 * so the legacy page size (and the paging selfchecks) keep working.
 */
const MIN_VISIBLE_ROWS = 3;
const MAX_VISIBLE_ROWS = 50;
const RESERVED_ROWS = 12;
const FALLBACK_VISIBLE_ROWS = 20;

/** Overlay page size for the current terminal, or the legacy fallback (20). */
export function overlayPageSize(tui: unknown): number {
	const rows = (tui as { terminal?: { rows?: number } })?.terminal?.rows;
	if (!rows || rows <= 0) return FALLBACK_VISIBLE_ROWS;
	return Math.max(MIN_VISIBLE_ROWS, Math.min(MAX_VISIBLE_ROWS, rows - RESERVED_ROWS));
}
