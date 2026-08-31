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
// Any compiler launch/crash failure, or diagnostic outside those two ownership
// boundaries, fails the check. The wrapper launches tsc itself so a shell pipe
// cannot hide the compiler's exit status.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const VENDOR_MARKER = "../../vendor/";
const vendorDrift = [];
let sawOwnError = false;
let sawUnexpectedError = false;

const defaultTscBin = fileURLToPath(
	new URL("../node_modules/typescript/bin/tsc", import.meta.url),
);
const tscBin = process.env.UC_TSC_BIN || defaultTscBin;
const result = spawnSync(process.execPath, [tscBin, "--noEmit"], {
	cwd: fileURLToPath(new URL("..", import.meta.url)),
	encoding: "utf8",
});

if (result.error) {
	console.error(`[check] TypeScript compiler failed to start: ${result.error.message}`);
	process.exit(1);
}

const output = [result.stdout, result.stderr].filter(Boolean).join("\n");
for (const line of output.split(/\r?\n/)) {
	if (!line) continue;

	if (line.startsWith("src/") && /error TS/.test(line)) {
		sawOwnError = true;
		console.log(line);
	} else if (line.includes(VENDOR_MARKER) && /error TS/.test(line)) {
		vendorDrift.push(line.trim());
	} else {
		if (/error TS/.test(line)) sawUnexpectedError = true;
		console.log(line);
	}
}

if (vendorDrift.length > 0) {
	console.log(
		`\n[check] ${vendorDrift.length} diagnostic(s) inside vendored oh-my-pi sources ` +
			"(upstream/bun-types drift — tolerated by policy, not ours to fix):\n" +
			vendorDrift.map((line) => `  ${line}`).join("\n"),
	);
}
if (sawOwnError) {
	console.error("\n[check] FAILED: type errors in packages/uc-orchestrator/src");
	process.exit(1);
}
if (sawUnexpectedError) {
	console.error("\n[check] FAILED: type errors outside the tolerated vendor boundary");
	process.exit(1);
}
if (result.signal || (result.status !== 0 && vendorDrift.length === 0)) {
	console.error(
		`\n[check] TypeScript compiler failed${result.signal ? ` with signal ${result.signal}` : ` with exit code ${result.status}`}`,
	);
	process.exit(1);
}

console.log("[check] uc-orchestrator src is type-clean.");
process.exit(0);
