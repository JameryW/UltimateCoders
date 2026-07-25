/**
 * Self-check for overlayPageSize — height-adaptive page size for UC overlays.
 * Run: bun run src/ui/overlay-pagination.selfcheck.ts
 *
 * ponytail: overlayPageSize drives cursor/scroll windowing in both overlays.
 * Wrong size = clipped footer or phantom cursor rows. Constants: RESERVED=12,
 * MIN=3, MAX=50, FALLBACK=20. Pin the clamp + fallback so a refactor can't
 * silently regress the window math.
 */
import { overlayPageSize } from "./overlay-pagination";

let failures = 0;
function check(name: string, cond: boolean) {
	console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
	if (!cond) failures++;
}

// undefined tui (selfcheck / headless) → legacy fallback 20
check("undefined tui → fallback 20", overlayPageSize(undefined) === 20);
check("null tui → fallback 20", overlayPageSize(null) === 20);
// tui without terminal.rows → fallback 20
check("tui without terminal.rows → fallback 20", overlayPageSize({}) === 20);
check("terminal.rows undefined → fallback 20", overlayPageSize({ terminal: {} }) === 20);
check("terminal.rows 0 → fallback 20", overlayPageSize({ terminal: { rows: 0 } }) === 20);
check("terminal.rows negative → fallback 20", overlayPageSize({ terminal: { rows: -5 } }) === 20);

// rows - RESERVED (12), clamped to [3, 50]
check("24-row terminal → 12 (24-12)", overlayPageSize({ terminal: { rows: 24 } }) === 12);
check("40-row terminal → 28 (40-12)", overlayPageSize({ terminal: { rows: 40 } }) === 28);
check("13-row terminal → 1 clamped to MIN 3", overlayPageSize({ terminal: { rows: 13 } }) === 3);
check("15-row terminal → 3 (15-12=3)", overlayPageSize({ terminal: { rows: 15 } }) === 3);
check("12-row terminal → 0 clamped to MIN 3", overlayPageSize({ terminal: { rows: 12 } }) === 3);
// MAX clamp: 100 rows → 100-12=88 clamped to MAX 50
check("100-row terminal → 88 clamped to MAX 50", overlayPageSize({ terminal: { rows: 100 } }) === 50);
check("62-row terminal → 50 (62-12=50)", overlayPageSize({ terminal: { rows: 62 } }) === 50);
check("63-row terminal → 51 clamped to MAX 50", overlayPageSize({ terminal: { rows: 63 } }) === 50);

// the OMP HookSelector formula mirrors Math.max(4, Math.min(15, rows-12)) — our
// bounds are wider (3..50) so overlays scale on big terminals too.
check("big terminal (200 rows) → MAX 50", overlayPageSize({ terminal: { rows: 200 } }) === 50);

console.log(`\n${failures === 0 ? "ALL PASS" : `${failures} FAILURE(S)`}`);
if (failures > 0) process.exit(1);
