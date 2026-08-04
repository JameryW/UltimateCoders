# Activate ResultAggregator in post-wave merge path

## Goal

Wire the dormant `ResultAggregator` (`agent/aggregator.py`) into the orchestrator's post-wave path so concurrent subtasks modifying the same file get an in-memory three-way merge (fast path) before git merge arbitration. Currently `orchestrator.py` has NO aggregate step — wave completion goes straight to MergeArbiter (cross-host git merge), and subtasks on the same worker editing the same file have no merge guard.

Also fix the latent data-loss bug in `_merge_file`: `changes[0].diff if changes[0].diff else base` — a subtask that modified a file but produced an empty `diff` field falls back to `base`, silently dropping its changes when merged against another modifier.

## What I already know

* `ResultAggregator` (`agent/aggregator.py`, 260 lines) — complete, tested (`test_aggregator.py`), but NEVER instantiated. `orchestrator.py` has zero references to `aggregate`/`ResultAggregator`.
* Orchestrator flow: subtasks complete → wave_end → (no aggregation) → `MergeArbiter.arbitrate(branches)` (Phase 2, opt-in via `UC_GIT_MERGE_ARBITRATE`).
* The aggregator's `_merge_file` (line 195): `current = changes[0].diff if changes[0].diff else base`. If `FileChange.diff == ""` (common — worker's `_parse_agent_file_changes` sets `diff=""` at sandbox.py:1007/1546), `current` falls back to base → the change is lost in the merge.
* `ConflictResolver` (4-tier: auto_merge → llm_assisted → reassign → human) is the merge engine, shared with MergeArbiter.
* `ResultAggregator` needs an `llm_client` for the LLM synthesis step (optional — works without).

## The gap

Two concerns:
1. **No in-memory merge**: same-worker concurrent subtasks editing the same file have no guard — the last writer wins, or files collide. MergeArbiter only handles cross-host (git branches). Within a wave, `DistributedConflictDetector` is advisory (same-process), so two subtasks on the same worker editing the same file can both "succeed" with conflicting content.
2. **Data-loss bug**: if the aggregator IS wired, the empty-diff fallback silently drops changes.

## Assumptions (temporary)

* The aggregator runs at wave completion (all subtasks in a wave have results), not per-subtask.
* It's a local in-memory merge — does NOT replace MergeArbiter (which handles cross-host git). They're complementary: aggregator merges within a worker's wave, MergeArbiter merges across workers.
* `base_files` (original file contents for the 3-way merge base) must be sourced — likely from the workspace's pre-wave state.

## Open Questions

* **Hook point confirmed**: `_update_task_status` (orchestrator.py:237) — fires when ALL subtasks complete (there's no wave concept; orchestrator is flat subtasks). The aggregator runs at line 237, BEFORE the MergeArbiter is scheduled (line 244). Both fire on full task completion.
* **base_files source?** The subtasks' workspace current file contents (pre-task) — needs the workspace handle. If no workspace (local execution), base_files = {} and the merge base is empty (acceptable — first-modifier-wins).
* **What does the aggregator OUTPUT do?** (still open — see below)
* **Fix the empty-diff bug in the same PR?** Yes — activating a buggy aggregator is worse than not activating.

## Open Question (output behavior)

The aggregator produces `AggregatedResult` (merged_files, conflict_files, status). On merge success, should it:
(a) Write merged content back to the workspace so MergeArbiter's git branches see the merged state (the aggregator runs first, then MergeArbiter merges branches — but if the aggregator already merged, the branches may have the merged content already), OR
(b) Just record conflicts (non-fatal) and let MergeArbiter handle all real merging — the aggregator is advisory, surfacing same-file conflicts early without writing?

Recommend (b): the aggregator is advisory — it surfaces conflicts (logs + records conflict_files) but does NOT write merged content. MergeArbiter remains the writer. This avoids double-writing / race between aggregator and MergeArbiter, and keeps the aggregator's role as "early conflict detection" not "writer".

## Requirements (evolving)

* Fix `_merge_file` empty-diff data-loss bug (don't fall back to base for an empty diff — treat empty diff as "no change recorded" and skip that modifier, OR error).
* Wire `ResultAggregator` into `_update_task_status` (orchestrator.py:237) — runs when all subtasks complete, BEFORE MergeArbiter is scheduled (line 244).
* base_files: source from subtasks' workspace if available; if no workspace (local), base_files = {} (empty base, first-modifier-wins).
* **Advisory only**: aggregator surfaces conflicts (logs + records conflict_files on the task) but does NOT write merged content. MergeArbiter remains the writer. Non-fatal — conflicts don't abort the task.
* Tests: the bug fix (empty-diff no longer loses data), the wiring (aggregator called at completion), advisory behavior (conflicts recorded, no write).

## Acceptance Criteria

* [ ] `_merge_file` empty-diff bug fixed + test
* [ ] `ResultAggregator` instantiated + called at wave completion in orchestrator
* [ ] base_files sourced from workspace
* [ ] Merged files written back to workspace on success
* [ ] Conflicts recorded (conflict_files) without aborting the task
* [ ] `pytest tests/python/test_aggregator.py` passes (existing) + new tests
* [ ] CI green

## Definition of Done

* Tests added
* Lint/CI green
* Docs: note the aggregator's role vs MergeArbiter in CLAUDE.md if the architecture doc changes

## Out of Scope

* LLM synthesis (the `llm_synthesis` field) — optional, defer if no llm_client wired
* Verification command (`verify_command`) — defer (needs a configured build/test cmd)
* Replacing MergeArbiter — they coexist

## Technical Notes

* `agent/aggregator.py:195` — the bug line
* `agent/aggregator.py:71` — `aggregate()` entry, takes `subtask_results` + `base_files` + optional `verify_command`
* `agent/conflict.py` — `ConflictResolver` (4-tier)
* `agent/orchestrator.py:239` — wave-completion / MergeArbiter scheduling area (the insertion point)
* `agent/workspace.py` — `WorkspaceManager` (base_files source)
* [[python-worker-audit-2026-08-03]] — flagged the latent bug + dormant status
