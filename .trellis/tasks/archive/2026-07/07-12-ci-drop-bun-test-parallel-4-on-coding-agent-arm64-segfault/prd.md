# CI: Drop `bun test --parallel=4` on coding-agent (arm64 segfault)

## Goal

Fix the one remaining failing TS CI job. `bun test (coding-agent)` crashes
on macos-latest (arm64) with `panic(main thread): Segmentation fault at
address 0x4` ~14min into the run, aborting all remaining OMP tests
(~627 reported "failed" but they're actually aborted, not asserted-failed).

## Root cause (verified from run 29159401030 logs)

The job runs `bun test --parallel=4 test src`. bun 1.3.14's parallel test
runner segfaults on macOS arm64 mid-suite. The crash kills the main thread;
every test scheduled after the crash point reports `aborted: worker panicked`
regardless of whether it would have passed. Most tests DO pass before the
crash (native tool inventory, TranscriptContainer, ACP agent, TanCommand all
green).

This is a bun-runner bug under `--parallel=4` on arm64, NOT an OMP test bug.

## Evidence

- `bun test (mnemopi)` uses `--parallel` (no count) → passes.
- `bun test (uc-orchestrator)` uses plain `bun test` (no parallel) → passes.
- Only `bun test (coding-agent)` uses `--parallel=4` → segfault.
- Crash log: `panic(main thread): Segmentation fault at address 0x4` then
  cascading `aborted: worker panicked`.

## Decision (locked)

- **D1: change `--parallel=4` → `--parallel`** (drop the explicit count).
  If bun's default parallelism still segfaults, fall back to no `--parallel`
  (sequential). Try `--parallel` first — it's the mnemopi-proven form.

## Assumptions

- Sequential/default-parallel run completes without segfault (bun's
  arm64 bug is count-sensitive or parallelism-sensitive).
- Coding-agent test suite takes longer sequentially but completes.
  Acceptable — correctness > speed for CI gate.

## Acceptance Criteria

- [ ] `ci-typescript.yml` coding-agent job no longer uses `--parallel=4`.
- [ ] coding-agent job completes (pass or real test failures, not segfault-abort).
- [ ] mnemopi + uc-orchestrator still pass (no change to them).

## Out of Scope

- Patching bun itself (upstream).
- Disabling specific OMP tests (only if a real assertion fails post-fix,
  not segfault-aborts).
