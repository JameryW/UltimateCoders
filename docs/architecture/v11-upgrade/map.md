# V11 Architecture Upgrade Map

This map records the v11 upgrades requested in the architecture ledger
(fifth period, items #25–#30). The implementations are additive and opt-in;
the Python feature flags are read per call and default off. The batch extends
the Rust engine, the worker retrieval path, and the Python acceptance and
knowledge libraries. It does not create a second scheduler, a second knowledge
authority, or a network service.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| Out-of-order speculative execution | `crates/uc-engine/src/tomasu_ooo.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `RunAheadController` / `TraceIrValidator` exported from `uc-engine` | module only |
| SWE-Shift mid-execution guard | `crates/uc-engine/src/shift_reconciler.rs`, `python/ultimate_coders/agent/shift_guard.py` | `UC_SHIFT_GUARD` | Python `ShiftGuard` mirror; no harness or gateway listener yet | module only |
| LatentSift candidate filter | `python/ultimate_coders/acceptance/latent_sift.py` | `UC_LATENT_SIFT` | acceptance library; no worker caller yet | module only |
| ANTE belief world model | `python/ultimate_coders/agent/ante_world_model.py` | `UC_ANTE_WORLD_MODEL` | agent library; no harness caller yet | module only |
| Rivet context shaper | `python/ultimate_coders/search/rivet_shaper.py` | `UC_RIVET_SHAPER` | worker `_build_search_context` after MemCredit / before rendering | wired |
| Chronos PR evolution graph | `python/ultimate_coders/knowledge/chronos_pr_graph.py` | `UC_CHRONOS_PR_GRAPH` | knowledge library; no worker caller yet | module only |

## TomasuLLM speculative execution (#25)

`crates/uc-engine/src/tomasu_ooo.rs` is the pure bookkeeping half of the
TomasuLLM design. It never touches an overlay, spawns a process, or executes a
tool. Callers describe a drafted `SpeculativeAction` and a
`CommittedObservation`; `RunAheadController` decides whether a speculative
result may be promoted.

The default `RunAheadConfig` follows the ledger: chain depth 4, 8 concurrent
speculative slots, scheduling weights long test 30 / short test 5 / syntax
check 3 / file read 1, and a bounded 4,096-entry audit ring. An action carries
a `Ready` operand when its file version is already materialised, or an
`InFlight` operand when it waits for a producer and may only be forked, never
executed early.

`TraceIrValidator::validate` applies the four trace predicates:
`V_act` (the action is unchanged), `V_dep` (the read/missing sets still
describe the workspace), `V_record` (the normalised observation matches), and
`V_effect` (the side-effect delta can be replayed). A rejected commit discards
the dependent suffix; an accepted commit records the action in the bounded
audit log. The audit exposes `accepted`, `rejected`, and
`mis-acceptance = 0` as a counter, not as a synthetic sample. No Rust caller
currently orchestrates a real copy-on-write overlay; that remains the
executor integration boundary.

## SWE-Shift mid-execution reconciliation (#26)

`crates/uc-engine/src/shift_reconciler.rs` models the three-step SWE-Shift
recovery protocol: change notification freezes writes and emits a structured
`ShiftEvent`; read-only refresh acknowledges the new state; history reset
requires at least one fresh observation in the current epoch before the first
edit. `ShiftKind` covers collaborator commits, interface changes, tool-session
rebinds, requirement revisions, and stale evidence. `WorkspaceEpoch` is the
externally observable identity `(git_head, tool_schema_epoch,
requirement_revision)`.

`ShiftReconciler` is a pure state machine with phases `Stable`, `Frozen`,
`RefreshPending`, `HistoryResetRequired`, `AwaitingFreshObservation`, and
`Ready`. `RecoveryPlan` lists the changed paths that must be observed and the
collaborator-added symbols that must be preserved; `verify_symbols_preserved`
rejects a patch that removes one of those symbols. The event log is bounded
(default 512).

`python/ultimate_coders/agent/shift_guard.py` mirrors the protocol for worker
or local-harness use with the same five shift kinds and six phases. It accepts
an epoch snapshot, classifies the dominant change (requirement revision takes
precedence over tool rebind, interface change, and collaborator commit), and
exposes `can_write`, `remaining_observations`, and `verify_symbols_preserved`.
No component currently polls a real workspace epoch or injects the guard into
the local harness; the module is deliberately a deterministic policy library
until a caller supplies epoch snapshots and fresh-observation events.

## LatentSift zero-token filtering (#27)

`python/ultimate_coders/acceptance/latent_sift.py` reuses hidden states that
the generator already produced. `ChannelTrace` stores per-step vectors in the
three channels `reasoning`, `observation`, and `function`, and
`time_weighted_pool` weights later steps more heavily before mean pooling.

An `ExperienceBank` holds positive and negative reference vectors per channel.
For each candidate and channel, the score is the nearest-neighbour margin
`d_neg - d_pos`, positive when the candidate is closer to the positive bank.
`LatentSiftFilter(top_k=8, blend_weight=0.5)` ranks each channel within the
candidate pool, uses the minimum channel rank as the bucket-principle
`distance_rank`, and blends that rank with a caller-supplied linear score. A
`SiftReport` contains the selected and dropped ids, per-channel margins, and
the oracle-retention fraction when an oracle candidate is declared. The module
does not run a model, infer missing hidden states, or execute a verifier; it
only orders candidates that a caller already produced. No worker currently
invokes it before Dockerless / SWE-Critix verification.

## ANTE belief world model (#28)

`python/ultimate_coders/agent/ante_world_model.py` adds the ANTE
anticipate-then-execute contract. Before an action the worker can emit an
`ActionPrediction` with `predicted_exit_code`,
`predicted_observation_digest`, and `confidence_decision` (`trust` or
`abstain`). After execution, `PredictionOutcome` is recorded and
`PredictionEvaluation.correct` requires both the exit code and the digest to
match.

`AnteWorldModel` tracks a bounded rolling accuracy estimate and computes the
ledger penalty `gamma = alpha * q* / (1 - q*)` from the target accuracy
`q*`. `should_trust` returns true only when the estimated accuracy exceeds the
target. `rank_candidates` sorts candidate actions by predicted utility, using
zero utility for abstaining candidates. `bapo_advantage(base_advantage,
prediction_reward, baseline_reward=0.5, coefficient=0.2)` anchors the
prediction reward into a policy advantage without pretending to be a full RL
trainer. `prediction_schema` exposes the three prediction fields for a tool
schema. No harness prompt currently asks the model for these fields.

## Rivet context shaping (#29)

`python/ultimate_coders/search/rivet_shaper.py` implements the three Rivet
stages. `extract_identifiers` pulls camelCase and snake_case identifiers from
the task statement. `RivetShaper.signal_grounding` scores each `EvidenceItem`
across four domains with weights path 1.6 / symbol 1.5 / sigdoc 1.2 / content
1.0, plus exact identifier matches. `utility_linking` expands along dependency,
usage, and configuration edges up to two hops, matching both `(path, symbol)`
keys and bare paths. `budget_shaping` packs items by priority and degrades
`Representation.SNIPPET` → `SIGNATURE` → `SUMMARY` instead of dropping
evidence when the budget is tight; the final fallback clips a summary to the
remaining budget.

`shape_context(task, pool, budget_tokens=8192)` returns a `RivetResult` with
shaped evidence, degraded count, and a rendered block. The worker integration
maps retrieval hits to `EvidenceItem` values with a `"[repo] path"` label and
passes `self._context_budget_tokens()` as the budget. The current
implementation is lexical and deterministic: it does not build a Tree-sitter
or SCIP dependency graph, so utility edges are declared in the item metadata
rather than inferred from a live repository index.

## Chronos PR evolution graph (#30)

`python/ultimate_coders/knowledge/chronos_pr_graph.py` distils merged pull
requests into `PRCard` records with the four ledger fields `summary`,
`signals`, `motivation`, and `solution`, plus files, authors, reviewers,
milestone, and timestamp. `ChronosPRGraph` stores cards and directed
`EvolutionEdge` relations across 11 kinds: file overlap, introduced-then-
modified, reference, backport, same-author follow-up, shared reviewer, same
milestone, revert, supersedes, depends-on, and related change. Edge weights
are clamped to `[1.0, 1.5]`.

`search(query, k=5)` finds entry cards; `expand(entry_ids, hops=3,
max_cards=10)` walks the graph and returns at most ten cards. `context_block`
renders a bounded historical block. For a complex node, `PatchCandidate`
records a change-agent or validation-strategy patch and its verification
flags. `EvolutionSteward.select(change, validation, history=())` requires
exactly two candidates, requires each to be verified, and selects exactly one
of them using historical motivation/solution support; it never invents a third
patch. The graph is in-memory and has no database or GitHub ingestion worker;
callers supply cards and edges from their own history store.

## Rollout and verification

All Python flags are read per call and default off. In-memory stores are
process-local and bounded. No database migration or new service is required.
The Rust additions are additive module exports.

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v11 -q --no-cov
# 34 passed, 3961 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/shift_guard.py \
  python/ultimate_coders/acceptance/latent_sift.py \
  python/ultimate_coders/agent/ante_world_model.py \
  python/ultimate_coders/search/rivet_shaper.py \
  python/ultimate_coders/knowledge/chronos_pr_graph.py \
  tests/python/test_v11_*.py
# All checks passed

cargo check -p uc-engine
# passed with rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 600 passed; 0 failed; 0 ignored
```

The Rust modules are rustfmt-clean. On Windows the test harness needs
`CARGO_PROFILE_DEV_DEBUG=0` and a workspace-local `TEMP`/`TMP` directory
because the MSVC `cl.exe` debug-record path fails on the very long default
temp path.

## Non-goals

- `tomasu_ooo` does not execute tools or manage an overlay; it is a decision
  and audit library for an executor that does.
- `shift_reconciler` / `shift_guard` do not watch Git or NATS by themselves;
  a caller must supply epoch snapshots and fresh-observation evidence.
- `latent_sift` does not compute hidden states or call a model.
- `ante_world_model` does not train a policy; `bapo_advantage` is a bounded
  scoring helper, not an RL loop.
- `rivet_shaper` does not infer a live dependency graph; callers declare
  dependency, usage, and configuration edges on each `EvidenceItem`.
- `chronos_pr_graph` does not ingest GitHub or persist cards; it is an
  in-memory graph and stewardship policy.
