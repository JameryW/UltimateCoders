# V12 Architecture Upgrade Map

This map records the v12 upgrades requested in the architecture ledger
(sixth period, items #31–#36). The implementations are additive and opt-in;
the Python feature flags are read per call and default off. The batch adds a
ledger module to the Rust engine and deterministic policy libraries to the
Python agent, acceptance, and knowledge packages. It does not create a second
scheduler, a model service, or a second knowledge authority.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| AutoCompact policy | `python/ultimate_coders/agent/auto_compact.py` | `UC_AUTO_COMPACT` | agent library; no harness caller yet | module only |
| HiSentinel triage | `python/ultimate_coders/agent/hi_sentinel.py` | `UC_HI_SENTINEL` | agent library; no harness or MCP gate yet | module only |
| Soei evidence ledger | `crates/uc-engine/src/soei_ledger.rs`, `crates/uc-engine/src/lib.rs` | n/a (additive Rust library) | `SoeiLedger` / `ProvEdge` exported from `uc-engine` | module only |
| CER prefix predictor | `python/ultimate_coders/acceptance/cer_predictor.py` | `UC_CER_PREDICTOR` | acceptance library; no worker caller yet | module only |
| Speculative Uncertainty gate | `python/ultimate_coders/agent/speculative_uncertainty.py` | `UC_SPECULATIVE_UNCERTAINTY` | agent library; no harness caller yet | module only |
| HSE skill evolver | `python/ultimate_coders/knowledge/hse_evolver.py` | `UC_HSE_EVOLVER` | knowledge library; no orchestrator caller yet | module only |

## AutoCompact policy (#31)

`python/ultimate_coders/agent/auto_compact.py` turns compaction into an
explicit policy decision rather than a fixed context-window threshold.
`AutoCompactPolicy.decide` chooses a `CompactTrigger`: `PHASE_COMPLETE` when
the current phase appears in `completed_phases`, `BUDGET_PRESSURE` at the hard
or soft token ratio, `ERROR_RECOVERY` in the repair phase with unresolved
errors, and `MANUAL` otherwise. Non-manual triggers obey a configurable
cooldown (`min_steps_between_compactions`, default 3).

`SummaryDraft.audit` detects the self-consistency failures the ledger calls
out: an empty summary, a missing next action, an error listed as both resolved
and unresolved, a completion action proposed while errors remain unresolved,
and touched files omitted from the summary. `AutoCompactPolicy.compact`
accepts only a consistent draft, appends a checkpoint, and returns monotonic
`TrajectorySegment`s whose advantage shares sum to one. Accepted checkpoints
are bounded by the policy object; no transcript is rewritten or persisted.

## HiSentinel pre-execution triage (#32)

`python/ultimate_coders/agent/hi_sentinel.py` provides the three-way
`ALLOW` / `REDIRECT` / `HARD_PAUSE` decision before a tool action executes.
`ActionProposal.risk` classifies actions as low, medium, or high risk.
`HiSentinel.review` applies the ledger ordering: a missing human preference is
a hard pause; a destructive action touching a protected file is a hard pause;
any protected-file write, destructive action without evidence, risky action
without evidence, or low-confidence action is redirected; everything else is
allowed.

`hgid_helpfulness_gate` implements the hindsight-gated distillation condition:
the privileged teacher must keep the same argmax label *and* improve its
confidence when future outcome information is added. `prefix_mode` marks
monitored steps as requiring the full causal prefix, avoiding the cross-step
KV-cache route flips reported in the ledger. The module does not call a model
or install an MCP middleware; it is the deterministic decision contract that a
caller can use for one.

## Soei evidence ledger (#33)

`crates/uc-engine/src/soei_ledger.rs` is a bounded, in-memory, tamper-evident
event ledger. `begin_record` stages a `PendingRecord`; `acknowledge` appends it
to the hash chain with a sequence number, BLAKE3 content digest, previous hash,
PROV edges, and a redaction flag. `record` combines the two phases for callers
that do not need an acknowledgement window.

`LedgerEventKind` covers mutations, verifications, proofs, completion claims,
and handoffs. `verify_chain` reports the first broken sequence, and
`verify_claim` enforces the L5 claim–proof separation: a completion claim must
be preceded by a mutation and by a proof or verification of that mutation. A
proof recorded after the claim does not satisfy the gate. `redact_secrets`
replaces recognizable credential shapes with `[REDACTED]` before hashing; pure
hex digests are preserved. The ledger has no database or NATS integration yet;
callers own persistence and ingestion.

## CER prefix predictor (#34)

`python/ultimate_coders/acceptance/cer_predictor.py` estimates a terminal
reward from a partial trajectory. `CerPredictor.build_rubric` combines a fixed
stage weight vector (localization 0.25, editing 0.30, verification 0.35,
repair 0.10) with tag relevance derived from historical tasks. Relevant
history raises the rubric weights and lowers the prune threshold.

`PrefixFeatures` records step, stage, test counts, required-file coverage,
verification presence, unresolved errors, and progress. `predict` computes a
bounded reward and a `CerPrediction` with the active rubric. Early termination
requires all of: a milestone has been reached, no test has passed, the reward
is below the rubric threshold, and progress is below 0.5. A passing test always
prevents pruning. `milestone_scores` returns one prediction per milestone for
a caller that samples at several checkpoints.

## Speculative Uncertainty gate (#35)

`python/ultimate_coders/agent/speculative_uncertainty.py` handles closed
adapters that expose neither hidden states nor logits. A local draft model can
run one forward pass over the already-generated tokens and report mean token
log-likelihoods for a reasoning span and an action span. The gate converts
those two `SpanLikelihood` values into `surprise = -mean_logprob`, applies
stage-specific weights (localization, editing, verification, repair), and
computes `risk = 1 - exp(-weighted surprise)`.

`SpeculativeUncertaintyGate.assess` emits a calibrated probability, the active
weights, and a veto only when the action span contains tokens and the risk
reaches the threshold. An empty action span is never vetoed. The module does
not run the draft model or parse provider responses; it defines the arithmetic
and the pre-execution contract.

## HSE skill evolver (#36)

`python/ultimate_coders/knowledge/hse_evolver.py` separates skill writing from
evaluation. `HseEvolver.write_candidate` records a `SkillCard` with name,
description, applicability, procedure, and measured `SkillEvidence` across
harnesses and models. `evaluate` keeps a card only when at least
`min_harnesses` (default 2) distinct harnesses reach 50% success and the
weighted success rate reaches `min_success_rate` (default 0.5). `evolve` sorts
accepted cards by weighted success and enforces `max_skills`.

`audit_performance` reports end-to-end latency recovery against the
MicroPerf-Bench target (42% by default) and renders the detected system-level
anti-patterns, including lock contention, N+1 queries, blocking I/O,
serialization hotspots, unbounded caches, missing indexes, chatty RPC, cold
start, and allocation churn. The module does not profile a process or train a
model; it is a curation and audit policy.

## Rollout and verification

All Python flags are read per call and default off. In-memory stores are
process-local and bounded. No database migration or new service is required.
The Rust addition is an additive module export.

Verification performed on 2026-11-23:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v12 -q --no-cov
# 36 passed, 3995 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/agent/auto_compact.py \
  python/ultimate_coders/agent/hi_sentinel.py \
  python/ultimate_coders/agent/speculative_uncertainty.py \
  python/ultimate_coders/acceptance/cer_predictor.py \
  python/ultimate_coders/knowledge/hse_evolver.py \
  tests/python/test_v12_*.py
# All checks passed

cargo check -p uc-engine
# passed with rsproxy sparse index and CARGO_PROFILE_DEV_DEBUG=0

cargo test -p uc-engine --lib
# 609 passed; 0 failed; 0 ignored
```

The Rust modules are rustfmt-clean. On Windows the test harness needs
`CARGO_PROFILE_DEV_DEBUG=0` and a workspace-local `TEMP`/`TMP` directory
because the MSVC `cl.exe` debug-record path fails on the very long default
temp path.

## Non-goals

- `auto_compact` does not rewrite the live context or call a summarizer; it
  validates a caller-produced summary and records checkpoint metadata.
- `hi_sentinel` does not train the 0.6B/1.7B sentinel or intercept MCP calls;
  it is the decision contract for a caller that does.
- `soei_ledger` does not persist to PostgreSQL or publish NATS events; it is an
  in-memory hash chain and claim gate.
- `cer_predictor` does not run a reward model; it is a deterministic rubric
  scorer over caller-supplied prefix features.
- `speculative_uncertainty` does not run the draft model or infer logits.
- `hse_evolver` does not execute skills or profile services; it curates cards
  and audits caller-supplied latency and anti-pattern evidence.
