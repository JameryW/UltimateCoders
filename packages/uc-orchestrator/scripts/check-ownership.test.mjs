import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const checkScript = fileURLToPath(new URL("./check-ownership.mjs", import.meta.url));
const crashingCompiler = fileURLToPath(
	new URL("./fixtures/tsc-crash.mjs", import.meta.url),
);

test("fails when the TypeScript compiler cannot complete", () => {
	const result = spawnSync(process.execPath, [checkScript], {
		encoding: "utf8",
		env: { ...process.env, UC_TSC_BIN: crashingCompiler },
	});

	assert.notEqual(result.status, 0);
	assert.match(result.stderr, /TypeScript compiler failed/);
});
