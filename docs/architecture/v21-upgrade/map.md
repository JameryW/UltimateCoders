# V21 Architecture Upgrade Map

This map records the v21 upgrades requested in the architecture ledger
(fifteenth period, items #85–#90). The additions are opt-in libraries:
two Rust modules in `uc-engine`, four Python components, and no new
service, database migration, scheduler, or live route. Every feature
flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Vulnerability propagation guard | `crates/uc-engine/src/vuln_guard.rs` | n/a (library) | patch review and memory audit; no engine caller yet | module only |
| LARK Lean 4 verifier | `python/ultimate_coders/acceptance/lark_lean_verifier.py` | `UC_LARK_LEAN_VERIFIER` | acceptance; no caller yet | module only |
| Enterprise DSL skill compiler | `python/ultimate_coders/agent/dsl_skill_compiler.py` | `UC_DSL_SKILL_COMPILER` | agent skill authoring; no worker caller yet | module only |
| Response store and discovery | `python/ultimate_coders/search/response_store.py` | `UC_RESPONSE_STORE` | retrieval/artifact exchange; no caller yet | module only |
| Requirement tracker | `python/ultimate_coders/agent/requirement_tracker.py` | `UC_REQUIREMENT_TRACKER` | task delivery and retry; no worker caller yet | module only |
| Continuous benchmark calibration | `crates/uc-engine/src/continuous_bench.rs` | n/a (library) | benchmark reporting; no engine caller yet | module only |

## Vulnerability propagation and memory audit (#85)

`vuln_guard.rs` has two deterministic surfaces. `VulnGuard::audit_memory`
checks caller-supplied `MemoryFile` contents against twelve `CweClass`
rules and returns a bounded `MemoryAuditReport`. `VulnGuard::review_patch`
registers `VulnerableSnippet` records and rejects a `PatchHunk` that
imitates a registered vulnerable pattern after whitespace/substring
normalisation; a matching memory rule can also block a patch. The
`final_disclosure` result reports the number of blocked snippets, memory
findings, and patch findings so the caller can disclose the audit outcome.
The module never edits a repository or calls an LLM.

## LARK Lean 4 segmented verification (#86)

`lark_lean_verifier.py` treats the GPU machine model as a one-time
registration. A `KernelSegment` has a name, source, postconditions, and
dependencies; `SegmentProof` records `proved`, `failed`, `unproved`, or
`skipped` with proof obligations. `LarkLeanVerifier::verify` requires a
registered model, non-empty unique segments, known dependencies, and
non-empty postconditions, then reports per-segment status. Level-2
verification additionally requires an explicit global-equivalence proof;
the module does not invoke a theorem prover.

## Enterprise DSL compile-edit loop (#87)

`dsl_skill_compiler.py` accepts a caller-supplied `compiler` callback and
an optional `editor`. `DslSkillCompiler::compile_edit` records every
attempt, keeps the best partial score, and stops at the first passing
result or when no editor is available. `distill_skill` compares
`SkillVariant` wording scores and reports the winner plus the delta from
the lowest or named baseline. `reformat` converts a `DslSkillPack` between
plugin and documentation forms. The module is deterministic and does not
generate code by itself.

## Response store and discovery database (#88)

`response_store.py` keeps large artifacts outside the prompt. `ResponseStore::put`
returns a compact `ArtifactHandle` with kind, summary, token estimate, and
metadata; `get` and `render` return the original text (with bounded
truncation for rendering), while `query` filters by kind and metadata.
`DiscoveryDatabase` stores `Candidate` records per cell, keeps the best
candidate per cell for `frontier`, and answers score-bounded queries. The
store is in-memory and never writes durable storage.

## Continuous requirement tracking (#89)

`requirement_tracker.py` models requirements in five states from pending
through clarifying, follow-up, verified, and failed. `RequirementTracker`
records clarifications, rejects verification while a clarification is
open, and exposes pending questions and a status report. `build_retry_plan`
always carries the raw failed trajectory and forces a full regression
suite; it refuses a summary-only retry. A retry plan does not run tests or
call a model.

## Continuous benchmark calibration (#90)

`continuous_bench.rs` versions each benchmark budget with a stable blake3
fingerprint over name, version, wall-clock, CPU, memory, and GPU limits.
`ContinuousBench` registers at most 64 specs, records results only against
the matching fingerprint, reports mean score and budget mismatch, and
attributes a gap to `Model`, `Harness`, or `Budget`. `audit_trajectory`
reports missing budget evidence and hidden-assumption findings. The module
does not execute benchmarks.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v21 -q --no-cov
# 41 passed, 4281 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/acceptance/lark_lean_verifier.py \
  python/ultimate_coders/agent/dsl_skill_compiler.py \
  python/ultimate_coders/search/response_store.py \
  python/ultimate_coders/agent/requirement_tracker.py \
  tests/python/test_v21_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness src/telemetry
# 182 pass, 0 fail

cargo test -p uc-engine --lib
# 793 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `vuln_guard` does not scan a live repository or rewrite code; callers
  supply memory files, snippets, and patches.
- `lark_lean_verifier` does not invoke Lean 4; callers supply proof
  status and obligations.
- `dsl_skill_compiler` does not generate DSL source; callers supply the
  compiler and editor callbacks.
- `response_store` is in-memory and does not upload artifacts.
- `requirement_tracker` does not run tests or call a model.
- `continuous_bench` does not execute a benchmark; it materialises the
  versioned budget and result ledger supplied by a caller.
