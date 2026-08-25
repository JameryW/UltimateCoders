// Ownership boundary for `pnpm run check`.
//
// The tsconfig `paths` mapping pulls @oh-my-pi/* from the vendored SOURCES,
// so tsc's program contains upstream implementation files. Those drift
// against the pinned bun-types from time to time (DOM shape changes etc.);
// they are upstream's responsibility, not ours to patch wholesale on every
// vendor sync.
//
// Policy enforced here:
//   - ANY diagnostic under our own src/**  -> exit 1 (we must be type-clean)
//   - diagnostics under ../../vendor/**    -> reported, tolerated (counted)
// Anything else (summary lines) passes through untouched.
//
// Usage: tsc --noEmit | node scripts/check-ownership.mjs

import { createInterface } from "node:readline";

const VENDOR_MARKER = "../../vendor/";
let vendorDrift = [];
let sawOwnError = false;

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
	if (line.startsWith("src/") && /error TS/.test(line)) {
		sawOwnError = true;
		console.log(line);
	} else if (line.includes(VENDOR_MARKER) && /error TS/.test(line)) {
		vendorDrift.push(line.trim());
	} else {
		console.log(line);
	}
});
rl.on("close", () => {
	if (vendorDrift.length > 0) {
		console.log(
			`\n[check] ${vendorDrift.length} diagnostic(s) inside vendored oh-my-pi sources ` +
				"(upstream/bun-types drift — tolerated by policy, not ours to fix):\n" +
				vendorDrift.map((l) => `  ${l}`).join("\n"),
		);
	}
	if (sawOwnError) {
		console.error("\n[check] FAILED: type errors in packages/uc-orchestrator/src");
		process.exit(1);
	}
	console.log("[check] uc-orchestrator src is type-clean.");
	process.exit(0);
});
