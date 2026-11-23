# V15 Architecture Upgrade Map

This map records the v15 upgrades requested in the architecture ledger (ninth
period, items #49–#54). The implementations are additive and opt-in; the Python
feature flag is read per call and defaults off. The batch adds two deterministic
modules to the Rust engine, one compression middleware to the Python agent, and
three planning libraries to the orchestrator (`harvest/`, `harness/`, and `rl/`).
It does not create a second scheduler, a model service, or a second knowledge
authority.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| SessionFarmer work episodes | `packages/uc-orchestrator/src/harvest/session_farmer.ts` | n/a (library) | orchestrator harvest planning; no live caller yet | module only |
| NanoHarness component router | `packages/uc-orchestrator/src/harness/nano_harness_router.ts` | n/a (library) | orchestrator harness planning; no live caller yet | module only |
| AttnCompress middleware | `python/ultimate_coders/agent/attn_compress_middleware.py` | `UC_ATTN_COMPRESS` | agent library; no worker caller yet | module only |
| SWE-Gate verifier | `crates/uc-engine/src/swe_gate_verifier.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `SweGateVerifier` exported from `uc-engine` | module only |
| OS-Bench migration gate | `crates/uc-engine/src/cross_os_migration_gate.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `CrossOsMigrationGate` exported from `uc-engine` | module only |
| SINKFLEX / MAGPIE / PTA-IRT | `packages/uc-orchestrator/src/rl/sinkflex_magpie_rl.ts` | n/a (library) | orchestrator RL accounting; no trainer caller yet | module only |

## SessionFarmer work episodes (#49)

`session_farmer.ts` clusters recorded `WorkTurn`s that share edited files into a
`WorkEpisode`. The cluster is deterministic: turn ids must be unique, files are
normalized, and episodes expose their sessions, files, commits, and whether any
turn carried tests. `SessionFarmer.harvest` reports tested and untested commits
per turn, so a test-bearing turn cannot make every commit in its episode look
tested.

`sealEpisode` refuses an episode without a commit and returns the minimum input a
blind solver may see. `CropRefiner.refine` runs the ledger's 2-round ×
3-version closure: each requirement-breaking mutant goes through a blind solver
and an independent quality judge, and a trial resolves only when both the
mechanical test and the judge pass. The report separates false positives from
false negatives and computes balanced accuracy over the expected-resolve labels.

## NanoHarness component router (#50)

`nano_harness_router.ts` is the model-tier and task-aware replacement for a
one-size-fits-all agent scaffold. Every plan keeps the structured tool registry,
keeps task-specific subagents for repository generation and repair, disables
generic subagents, disables lossy compaction, and enables the didactic
compensation prompts only for the smallest model tier. `COMPONENT_GAINS` records
the published absolute gains; `COMPONENT_ORDER` fixes the serialization order.

`guideMcpError` converts an MCP/tool failure into one executable next action:
path verification for `ENOENT`, permission inspection for `EACCES`, bounded
retry for timeouts, exact-registry lookup for unknown tools, and schema
validation for invalid arguments. An unclassified error stays honest: it is
marked non-executable instead of inventing a repair.

## AttnCompress middleware (#51)

`attn_compress_middleware.py` implements the three-stage compression pipeline.
`PplSpikeChunker` tracks delimiter depth and quote state so a chunk never ends
inside a JSON object or code block; a chunk whose perplexity exceeds the running
median by `spike_ratio` is marked as a spike. `AttentionScorer` blends lexical
focus overlap, caller-supplied proxy attention, and recency, with a small floor
for structural chunks.

`RollingRecallWindow` keeps the highest-scoring chunks inside the token budget
and retains every suppressed chunk. `on_focus_change` re-scores that retained
history and returns the suppressed chunks that are relevant again, so an early
observation can be recalled without re-reading it. `CompressionResult` reports
the chunk list, active/suppressed sets, recalled ids, token counts, and the
compression ratio. The middleware is pure bookkeeping; it never calls a proxy
model.

## SWE-Gate verifier (#52)

`swe_gate_verifier.rs` separates the functional suite from independently
executable review constraints. `ReviewConstraint` covers architecture layering,
API compatibility, complexity bounds, forbidden calls, and security invariants;
`SweGateVerifier.check_patch` evaluates the text predicates and
`check_complexity` evaluates a measured ceiling. `evaluate` requires functional
tests, every constraint observation, and a clean `RewardHackSignals` before the
patch can be `resolved`.

`RewardHackScrubber.scan` reports the four clean-room leak channels from
SWE-Bench Pro Verified (`.git/logs`, remote tags, future commits, inline test
answers) without deleting anything. The verifier never executes tests or
rewrites a patch; it is the dual gate that rejects the 34.3% of functionally
passing patches that violate review constraints.

## OS-Bench migration gate (#53)

`cross_os_migration_gate.rs` compares Linux/Docker and Windows/PyWinpty
coverage. `CoverageRegion` records one line range in one of the five OS-Bench
classes (path separator, line ending, process control, filesystem semantics,
native API); `compare_coverage` returns every region with unequal hit counts and
marks one-sided coverage as platform-specific.

`CrossOsMigrationGate.evaluate` requires the target-platform tests and the
counter-platform regression suite to pass, and blocks a patch that leaves a
platform-specific region unexercised on either substrate. The counter suite can
be waived explicitly for diagnostics, but the report still records both
outcomes.

## SINKFLEX / MAGPIE / PTA-IRT (#54)

`sinkflex_magpie_rl.ts` evaluates the zero-value sink identity
`alpha_sink = sigmoid(ell - s_eta)`, `O_sink = alpha_sink * O_std` with a
numerically stable sigmoid. `memoryEstimate` compares measured peak-memory
values and reproduces the published 19.7% reduction at 4,096 tokens.

`magpieAggregate` normalizes positive role weights and returns a monotone
weighted preference value. `ptaIrtEstimate` performs a one-parameter logistic
update with trace-weighted evidence and returns ability, evidence, and standard
error; `ptaBenchmarkScore` maps the latent ability back into `[0, 1]`.
`SinkFlexMagpieTrainer` is the convenience facade for all three.

## Verification

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v15 -q --no-cov
# 13 passed, 4107 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/attn_compress_middleware.py \
  tests/python/test_v15_attn_compress_middleware.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness
# 116 pass, 0 fail

cargo check -p uc-engine
# passed with the rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 680 passed; 0 failed; 0 ignored

cargo fmt -p uc-engine -- --check
# clean
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory because the MSVC `cl.exe` debug-record
path fails on the very long default temp path.

## Non-goals

- `session_farmer` does not replay a live session or start a sealed container;
  it clusters caller-supplied turns and runs caller-supplied solver/judge
  callbacks.
- `nano_harness_router` does not execute a harness or edit a model prompt; it
  returns a component plan.
- `attn_compress_middleware` does not load a proxy model; the caller supplies
  the attention scores.
- `swe_gate_verifier` does not run tests or remove files; it consumes
  caller-supplied functional/review evidence.
- `cross_os_migration_gate` does not execute either platform's tests; it
  consumes measured outcomes and coverage.
- `sinkflex_magpie_rl` does not implement a FlexAttention kernel or train a
  policy; it is the deterministic accounting layer.
