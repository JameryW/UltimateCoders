# V29 Architecture Upgrade Map

This map records the v29 upgrades requested in the architecture ledger
(twenty-third period, items #133–#138, the final batch). The additions are
opt-in libraries: two Rust modules under existing subsystems, three Python
components, one TypeScript credit layer, and no new service, database
migration, scheduler, container runtime, or live route. Every feature flag
defaults off.

## Status

| Area | Modules | Flag | Wired at | State |
| --- | --- | --- | --- | --- |
| GrepMem log and Memorix views | `python/ultimate_coders/memory/grepmem_memorix.py` | `UC_GREPMEM_MEMORIX` | `memory.py` write journal + lexical search fallback | wired (opt-in) |
| TriGRPO sibling tree credit | `packages/uc-orchestrator/src/rl/trigrpo_tree_credit.ts` | n/a (library) | RL trace export; no caller yet | module only |
| Focus resumption and behavior fingerprint | `python/ultimate_coders/architecture/focus_resumption.py`, `python/ultimate_coders/agent/focus_context.py` | `UC_FOCUS_RESUMPTION`, `UC_FOCUS_CONTEXT` | in-flight cards for sibling subtasks in the prompt | wired (opt-in) |
| Weakest-subagent safety barrier | `crates/uc-engine/src/governance/subagent_safety_barrier.rs` | n/a (library) | delegation gate; no engine caller yet | module only |
| OverServe polling and counter-evidence guard | `crates/uc-engine/src/scheduler/overserve_evidence_guard.rs` | n/a (library) | tool scheduling; no engine caller yet | module only |
| Repository-grounded rubrics and collusion audit | `python/ultimate_coders/acceptance/repo_rubric_scorer.py`, `python/ultimate_coders/agent/rubric_audit.py` | `UC_REPO_RUBRIC_SCORER`, `UC_RUBRIC_AUDIT` | advisory per-attempt rubric event | wired (opt-in) |

## GrepMem log and Memorix views (#133)

`grepmem_memorix.py` keeps memory lossless and cheap. `GrepMemStore` is an
append-only text log searched with a caller-supplied regular expression; it has
no embedding model or vector index. `VersionedViewStore` pins a precomputed view
to an exact repository commit so a later run can reuse the view built for that
tree instead of rebuilding it. `detect_evidence_loss` implements the SELD check:
terms required by the task but absent from a retained summary are reported as
silent evidence loss. `RefusalCompressionGuard.should_escalate` flags a lossy
compression that dropped a refusal/constraint term. `GrepMemMemorix` is the
facade that ties the log, views, and audit together. Callers own persistence;
the module stores in memory and never calls a model or database.

Production wiring (memory layer): `memory.py` arms a process-local
`GrepMemMemorix` journal when the flag is set. Every successful
`ShortTermMemory.write` / `LongTermMemory.write` appends the written entry to
that append-only log, and `LongTermMemory.search` falls back to a lexical
recall over the journal when the semantic index returns nothing. The journal is
a projection, never the authority: the engine write still happens first, a full
journal drops the record instead of failing the write, and semantic results
always win. `recall(pattern)` exposes raw regex recall on both layers;
`reset_recall_journal()` drops the process-wide journal.

## TriGRPO sibling tree credit (#134)

`trigrpo_tree_credit.ts` extends the existing `TriGrpoTree` rather than
duplicating its three-group statistics. `TriGrpoTreeCredit` accepts
`CreditBranch` records and, when a successful action is discovered late,
`addSuccessfulSibling` replays that success as a sibling at the same parent and
decision state. The failed branches then receive a counterfactual comparator
instead of only a terminal zero. `tokenCredit` distributes a branch advantage
across its response tokens and honours an optional loss mask; an all-false mask
returns zeros. The layer is deterministic and performs no rollout, sampling, or
training.

## Focus resumption and behavior fingerprint (#135)

`focus_resumption.py` keeps one `TaskStateCard` per branch/issue and filters a
resumed turn to history relevant to that card. `FocusResumption.switch` records
what was kept and dropped and updates the active task. `BehaviorFingerprint`
reports exploration / editing / testing proportions from `BehaviorEvent`s
without using task outcomes, token counts, or tool names; `drift_from` compares
a live profile with a baseline. The module is deterministic bookkeeping and the
caller supplies the events.

**Production wiring (worker context).** `python/ultimate_coders/agent/focus_context.py` keeps a
process-local registry of the subtasks a task graph has in flight. A subtask
registers its card before its prompt is assembled, a sibling of the same graph
receives a relevance-filtered `focus-resumption` evidence item naming the other
open work and its files, and finishing retires the card and emits a
`focus resumption` progress event with the remaining count. Only cards that share
vocabulary with the current description are spelled out - the rest collapse to
one `N subtask(s) are changing unrelated files` line - so the block cannot crowd
out the subtask's own targets. Both `UC_FOCUS_CONTEXT` and
`UC_FOCUS_RESUMPTION` must be on; with either off the prompt is byte-identical.

## Weakest-subagent safety barrier (#136)

`subagent_safety_barrier.rs` enforces two hard rules. A sensitive task that
reads untrusted files cannot be delegated to a weak child unless the child
returns raw evidence to the orchestrator; a summarized answer is folded.
Fan-out is allowed only when at least one compensating condition is present:
partitioned evidence, external verification, or heterogeneous model-family
errors. Without one, `evaluate` returns an allowed-but-folded decision so the
caller runs one deep agent. The module does not dispatch work or call a model.

## OverServe polling and counter-evidence guard (#137)

`overserve_evidence_guard.rs` covers three long-horizon failure modes.
`PollThrottle` applies exponential backoff to repeated same-state polls and
opens a hard breaker when the completed state is observed. `CounterEvidenceGuard`
marks a completion claim for rollback as soon as any contradicting evidence is
observed. `ProviderCrashFuse` denies framework-synthesized completion after a
provider content-filter crash, so a fabricated final answer cannot be emitted.
`OverServeGuard` is the facade over all three. The module has no clock and no
randomness; callers supply timestamps/observations and decide how to roll back.

## Repository-grounded rubrics and collusion audit (#138)

`repo_rubric_scorer.py` freezes weighted binary rubric items from repository
evidence before editing, then scores a patch without executing the repository.
`RepoGroundedRubricScorer.score` accepts only a score with no missing required
items, a weighted score of at least 0.6, and a clean test report.
`rank` computes AUROC between a positive and a negative patch. The module also
separates a real decision-state collusion monitor from the brittle
instruction-text probe: `DecisionStateMonitor.instruction_probe` reports a weak
signal when its AUROC is below 0.8, while `decision_monitor` scores actual
approve/deny decisions. The module performs no model call and launches no
container.

**Production wiring (worker acceptance).** `python/ultimate_coders/agent/rubric_audit.py` is the
missing adapter: it freezes one item per dimension (correctness required, then
test coverage, compatibility, security, maintainability) and derives each
feature from deterministic checks on the attempt's own evidence - added/removed
lines, changed paths versus the declared file constraints, a small hazard list
(`eval`/`exec`, `shell=True`, `os.system`, `pickle.loads`, `yaml.load`),
added `TODO`/`FIXME` markers and bare `except:`. The worker emits a
`rubric audit` progress event after the observed test delta, so the score can
see the F2P/P2P sets when tests actually ran. Two honest limits are reported
rather than hidden: `rubric_passed` stays false on an execution-free attempt
(the scorer demands a clean test report), so a no-test repository is judged by
`rubric_weighted_ok`; and the event carries counts and markers only, never
diff text or file bodies. Advisory throughout - it never rejects a subtask.

## Verification

Verification performed on 2026-11-24:

```bash
.venv/Scripts/python.exe -m pytest tests/python -k v29 -q --no-cov
# 30 passed, 4546 deselected

.venv/Scripts/python.exe -m ruff check \
  python/ultimate_coders/memory/grepmem_memorix.py \
  python/ultimate_coders/architecture/focus_resumption.py \
  python/ultimate_coders/acceptance/repo_rubric_scorer.py \
  tests/python/test_v29_*.py
# All checks passed

cd packages/uc-orchestrator && bun test src/rl src/skills src/harvest src/harness src/telemetry
# 257 pass, 0 fail

cargo test -p uc-engine --lib
# 951 passed; 0 failed; 0 ignored
```

On Windows the Rust test harness needs `CARGO_PROFILE_DEV_DEBUG=0` and a
workspace-local `TEMP`/`TMP` directory, plus the rsproxy sparse-index config.
`cargo fmt -p uc-engine -- --check` is clean.

## Non-goals

- `grepmem_memorix` does not persist the log or build embeddings; storage and
  retrieval scope belong to the caller.
- `trigrpo_tree_credit` does not collect rollouts or update model weights.
- `focus_resumption` does not summarize with a model; it filters and stores
  caller-supplied cards and events.
- `subagent_safety_barrier` does not dispatch or schedule subagents.
- `overserve_evidence_guard` does not own MCP tools, clocks, or rollback
  execution.
- `repo_rubric_scorer` does not run tests, call a judge model, or contact a
  provider.
