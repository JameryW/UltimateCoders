# V20 Architecture Upgrade Map

This map records the v20 upgrades requested in the architecture ledger
(fourteenth period, items #79–#84). The additions are opt-in libraries:
two Rust modules in `uc-engine`, three Python components, and one
orchestrator telemetry helper. No new service, database migration,
scheduler, or live route is introduced, and every feature flag defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Harness awareness routing | `crates/uc-engine/src/harness_awareness.rs` | n/a (library) | tool dispatch; no engine caller yet | module only |
| Rate-limited explicit state | `python/ultimate_coders/search/state_filter.py`, `python/ultimate_coders/agent/state_ledger.py` | `UC_STATE_FILTER`, `UC_STATE_LEDGER` | run-state snapshot and thrash signal per write/command | wired (opt-in) |
| MemSidecar scheduling | `crates/uc-engine/src/mem_sidecar.rs` | n/a (library) | memory retrieval scheduling; no engine caller yet | module only |
| EvoGroup shared harness | `python/ultimate_coders/agent/evogroup_harness.py` | `UC_EVOGROUP_HARNESS` | group-agent harness; no orchestrator caller yet | module only |
| SWE-WhatIf replay | `python/ultimate_coders/acceptance/swe_whatif_replay.py` | `UC_SWE_WHATIF_REPLAY` | evaluation; no acceptance caller yet | module only |
| Behavioral fingerprint | `packages/uc-orchestrator/src/telemetry/behavior_fingerprint.ts` | n/a (library) | telemetry analysis; no live caller yet | module only |

## Harness awareness telemetry routing (#79)

`harness_awareness.rs` turns verified tool health telemetry into a
pre-dispatch routing decision. `ToolObservation` carries the endpoint,
success flag, latency, fault-injection state, and a `verified` bit. Only
verified observations enter a bounded `ToolHealthSnapshot`; unverified
observations are ignored.

`HarnessAwarenessRouter::route` scores each `ToolCandidate` with
`success_probability`, `expected_latency_ms`, and
`expected_regret_ms = max(base_latency, p95_latency) * (1 - success_rate)
+ fault_penalty`. A fault-injected endpoint receives the configured
`DEFAULT_FAULT_PENALTY_MS` (10,000 ms), and ties are broken by endpoint id.
The module is deterministic and in-memory; it does not call a network or
an LLM.

## Rate-limited explicit state filtering (#80)

`state_filter.py` separates parsing from state mutation. The LLM-facing
surface only produces a `ParsedEvent` from a single line such as
`key=value@timestamp`; `ExplicitStateFilter` is the sole writer and applies
the event through a deterministic rule engine.

Stale timestamps are rejected, dependency keys are recorded, and a
bounded key count evicts the oldest entry deterministically. `snapshot`
returns the current key/value projection without exposing parser state.
The module never writes durable storage; callers supply event text and
consume the projection.

**Production wiring (local harness).** `agent/state_ledger.py`
(`UC_STATE_LEDGER`, which also requires `UC_STATE_FILTER`) is the caller the
item was missing. The harness applies one typed event per observable fact —
`file:<path>` after a write, `command:last` and `command:<digest>` after a
command exit — and keeps a bounded keyed snapshot instead of re-deriving run
state from the transcript. The only judgement it makes on its own is thrash: a
path rewritten three times with no passing command in between is reported once
per checkpoint as a `state_ledger` event, and a zero exit code clears the
counts. It is observation only and never blocks a tool; the snapshot is also
returned in the final envelope so a caller can read the run's own state without
parsing the transcript.

## MemSidecar scheduling (#81)

`mem_sidecar.rs` keeps memory retrieval off the agent's critical path.
`HookRule` maps a tool event to memory ids; `on_event` returns the
registered memories. `PrefetchPrediction` feeds a bounded Foresight
prefetch cache; the cache keeps only the highest-probability entries and
is rebuilt after each insertion so `prefetched_count` reflects the bound.

`MemSidecar::route` implements the OPSD choice between a verified weight
memory and a text fallback. A weight record is trusted only when the
verification flag is set and `confidence >= confidence_threshold` (default
0.6); otherwise the text route is reported. `observe_blocked_ms` records
the latency removed from the main loop. The module is a pure state
machine and does not call an LLM.

## EvoGroup shared-context harness (#82)

`evogroup_harness.py` starts from a six-tool `DEFAULT_TOOLS` cold start.
`TrajectorySelector` keeps high-information traces, admits low-confidence
traces for reflection, and rejects duplicates. `ProgressiveDisclosureAnalyzer`
renders a summary, a bucketed view, or the full text depending on the
caller's disclosure budget.

`SharedHarness` stores versioned instructions and `EvoGroupHarness` turns
accepted traces into `HarnessMutation` records. The module adds the
low-confidence reflection instruction and a summary-disclosure rule; it
does not call a model or rewrite a live agent configuration.

## SWE-WhatIf counterfactual replay (#83)

`swe_whatif_replay.py` extracts `PushbackPoint` records from a
`RecordedSession` by scanning user turns that mark a correction. A policy
callback is replayed against those points and `ReplayReport.avoidance_rate`
reports how often the policy would have avoided the user's pushback.

`evaluate_organization` applies the same evidence model to
`OrganizationRole` and `OrganizationTask` records, producing an
`OrganizationReport` with per-role success and a cross-role total. The
module is deterministic and does not record or replay live user data.

## Behavioral trajectory fingerprinting (#84)

`behavior_fingerprint.ts` deliberately ignores outcomes, token counts, and
tool names. `behaviorProfile` counts search/edit/test phases and builds a
deterministic transition string plus an FNV-1a fingerprint.
`compareProfiles` returns a positional similarity, and `CodeNibContext`
stores and renders multi-view repository snapshots.

`SkillBuffBench` models the three-level skill axis (none / curated /
auto-library) against memory mode and runs a deterministic paired
bootstrap with a seeded mulberry32 generator. The module does not read a
repository or run a benchmark.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v20 -q --no-cov
# 30 passed, 4251 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/search/state_filter.py \
  python/ultimate_coders/agent/evogroup_harness.py \
  python/ultimate_coders/acceptance/swe_whatif_replay.py \
  tests/python/test_v20_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness src/telemetry
# 182 pass, 0 fail

cargo test -p uc-engine --lib
# 771 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory. `cargo fmt -p uc-engine -- --check`
is clean.

## Non-goals

- `harness_awareness` does not run health checks or speak to tools; callers
  provide verified observations.
- `state_filter` does not persist state or call an LLM.
- `mem_sidecar` does not connect to a memory server; it schedules records
  supplied by the caller.
- `evogroup_harness` does not mutate a running shared agent; it returns
  versioned harness instructions for a caller to apply.
- `swe_whatif_replay` does not collect live sessions; callers supply
  recorded turns.
- `behavior_fingerprint.ts` does not run a benchmark or read a repository.
