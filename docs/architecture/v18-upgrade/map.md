# V18 Architecture Upgrade Map

This map records the v18 upgrades requested in the architecture ledger
(twelfth period, items #67–#72). The additions are opt-in or additive
libraries: three deterministic modules in `uc-engine`, four Python
components, one orchestrator RL helper, and one dashboard state library.
No new service, database migration, scheduler, or live UI route is
introduced.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| ChainSWE chain isolation | `crates/uc-engine/src/chain_isolation.rs`, `python/ultimate_coders/agent/chain_editor.py` | `UC_CHAIN_EDITOR` (Python) | multi-bug maintenance; no live caller yet | module only |
| RST recursive synthesis | `packages/uc-orchestrator/src/rl/rst_synthesizer.ts` | n/a (library) | offline RL task synthesis; no live caller yet | module only |
| SetupX LIFO setup | `crates/uc-engine/src/setupx_lifo.rs`, `python/ultimate_coders/search/xpu_store.py` | `UC_XPU_STORE` (Python) | environment setup; no live caller yet | module only |
| PROBE failure recovery | `python/ultimate_coders/agent/probe_recovery.py`, `python/ultimate_coders/agent/probe_brief.py` | `UC_PROBE_RECOVERY`, `UC_PROBE_BRIEF` | worker step bridge (`failure_diagnosis` in `{{prev_outputs_json}}`) | wired (opt-in) |
| AgentStepper micro-commits | `crates/uc-engine/src/micro_commit.rs`, `dashboard/src/lib/agentStepper.ts` | n/a (library) | dashboard event inspection; no UI panel yet | module only |
| SWE-CI continuous integration | `python/ultimate_coders/acceptance/swe_ci_loop.py` | `UC_SWE_CI_LOOP` | acceptance loop; no runner caller yet | module only |

## ChainSWE chain isolation (#67)

`chain_isolation.rs` models a multi-bug maintenance chain as scoped patches
against successive repository states. A `BugScope` names the target paths,
symbols, and required symbols for one bug. `ChainPatch` carries `AstEdit`
entries with path, symbol, added lines, and removed lines.

`ChainSanitizer.sanitize` keeps only edits whose path is in the scope and
whose symbol is a scope symbol, strips out-of-scope edits as overshoot, and
reports required symbols that never appear as undershoot. `survival` is
`exp(-lambda * overshoot_lines)`, and `chain_survival` multiplies the
per-step survival values. The module never parses source text or touches a
worktree; callers provide AST-level edit records.

`chain_editor.py` mirrors the same contract for Python orchestration:
`ChainEditor.sanitize` returns a `SanitizedPatch`, `summarize` emits a
structured `ChangeSummary` for the next bug, and `chain_survival` reports the
same exponential retention signal. The edit summary is the only state that
should cross a bug boundary; the raw trace is intentionally not forwarded.

## RST recursive synthesis (#68)

`rst_synthesizer.ts` implements solution-first quadruple recursion. A
`Quadruple` contains the workspace (`workspace`), reference solution
(`solution`), verifier (`verifier`), and public instruction (`instruction`).
`proveQuadruple` requires the reference solution to pass (`V(Exec(W,S)) = 1`)
and the untouched workspace to fail (`V(W) = 0`), so every accepted task has a
machine-checkable proof of solvability and non-triviality.

`advanceQuadruple` extends the solution first, keeps the proof, then derives
the next instruction. `RSTQuadrupleSynthesizer.step` returns a numbered
accepted task; `generate` produces the configured number of rounds with
growth in solution length and command count. `estimatedCost` uses the
published `$0.05` per-task unit. The module is a deterministic task builder;
it does not execute a verifier.

## SetupX LIFO setup (#69)

`setupx_lifo.rs` represents an environment experience unit as `Xpu` with a
signature, textual guidance, and ordered actions. `SetupXLifo.speculate`
selects the highest-posterior XPU, pushes a `Snapshot`, and records a pending
`Speculation`. `judge` accepts or rejects the speculation using
`SetupEvidence` (dynamic link, ports, smoke test); a rejected speculation
pops the snapshot, while an accepted one keeps the checkpoint.

`posterior` uses a Beta-style success ratio with a prior and is bounded by
the registered XPU history. `rollback` is only allowed when idle with a
non-empty snapshot stack. The module never invokes Docker; callers perform
the actual snapshot and probe operations. `xpu_store.py` mirrors the same
protocol with `XpuStore`, `SnapshotStack`, and `SetupXRunner` for Python
workers and explicitly preserves a custom empty stack instead of silently
replacing it.

## PROBE failure recovery (#70)

`probe_recovery.py` implements the four-stage loop
`Telemetry(T) -> Evidence(E) -> Diagnosis(D) -> Guidance(G)`.
`ProbeRecoveryEngine.extract` normalizes a failed attempt into `Telemetry`;
`diagnose` produces a `Diagnosis` with a main cause, failure anchor,
behavioral error, and confidence in `[0, 1]`; `guide` builds bounded
`Guidance`; and `recover` returns a `RecoveryReport`.

`OracleSignals` quantifies the five missing-signal dimensions:
`S_repro`, `S_regr`, `S_loc`, `S_ctx`, and `S_api`. `GuidanceGate` rejects
guidance that is not grounded in collected evidence, not actionable, or wider
than the intervention scope. The engine is deterministic and never calls a
model; callers supply observations and oracle snapshots.

**Production wiring (worker step bridge).** `agent/probe_brief.py`
(`UC_PROBE_BRIEF`, which requires `UC_PROBE_RECOVERY` too) projects the failed
`AgentOutput` the worker already holds onto PROBE telemetry: `test_output` is
preferred over `stderr_tail`, file changes become `touched_files`, and commands
that look like test runs become `test_names`. `Worker._output_to_json` adds the
resulting brief under `failure_diagnosis` for failed steps only, so the next
step in a multi-step subtask (the `{{prev_outputs_json}}` bridge) receives an
anchored cause, failure anchor, behavioral error, signal gaps and up to three
bounded recovery actions instead of a bare stderr tail. The brief is capped at
`MAX_BRIEF_CHARS = 1200` encoded characters with per-field truncation, so it
cannot crowd out the step summary it travels with.

## AgentStepper micro-commits (#71)

`micro_commit.rs` exposes an `AgentEvent` stream with before/after LLM-query
and tool-call kinds. `AgentStepper` supports `Paused`, `Stepping`, and
`Running` states, breakpoints, resume/pause/step, and `hot_edit` of prompts,
responses, tool arguments, and tool results. `ingest` appends one shadow
`MicroCommit` after each `AfterToolCall` event; the commit records the event
sequence, parent, summary, and diff. The module never writes to the target
repository; callers own the shadow `git commit-tree` backend.

`dashboard/src/lib/agentStepper.ts` mirrors the state machine and shadow
commit bookkeeping for the React dashboard. The optional AgentStepperPanel UI
is intentionally deferred: the current delivery is the deterministic state
library and its test harness, so the dashboard can consume it without a new
route.

## SWE-CI continuous integration (#72)

`swe_ci_loop.py` models a 71-commit CI maintenance sequence. `ArchitectAgent`
checks the architecture invariant set and a module-coupling ceiling;
`ProgrammerAgent` applies a local change. `SweCiLoop.run` rejects a commit
that loses any previously passing test, using the monotone
`PassSet(c_t) superset-of PassSet(c_{t-1})` contract, and reports the
zero-regression rate over the run.

`CiCommit`, `ArchitectVerdict`, `CommitDecision`, and `CiReport` are plain
dataclasses. The module does not run git, tests, or containers; callers
provide commit outcomes and coupling measurements.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v18 -q --no-cov
# 40 passed, 4179 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/chain_editor.py \
  python/ultimate_coders/search/xpu_store.py \
  python/ultimate_coders/agent/probe_recovery.py \
  python/ultimate_coders/acceptance/swe_ci_loop.py \
  tests/python/test_v18_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness
# 165 pass, 0 fail

cd dashboard && node --test tests/agentStepper.test.mjs
# 10 pass

cargo test -p uc-engine --lib
# 742 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `chain_isolation` does not parse a real AST, apply a patch, or run a
  verifier; callers supply `AstEdit` records.
- `rst_synthesizer` does not execute a verifier or generate a workspace; it
  maintains the quadruple/proof invariant for a caller-owned environment.
- `setupx_lifo` does not invoke Docker, OverlayFS, or a network probe; the
  caller performs the snapshot and passes `SetupEvidence`.
- `probe_recovery` does not call an LLM or query a repository; callers supply
  telemetry and oracle signal coverage.
- `micro_commit` does not create a real Git object; the caller-backed shadow
  branch is a deployment concern.
- `swe_ci_loop` does not run tests or a CI service; it evaluates supplied
  commit outcomes against the monotone pass-set contract.
