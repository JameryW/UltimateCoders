# Wire verify_command through scheduler config to aggregator

## Goal

The ResultAggregator's `_verify(command)` (aggregator.py:263) runs a verification command (build/test) after aggregation. But no one passes `verify_command` — the orchestrator calls `aggregate()` (line 506) without it (#550 PRD deferred it). The missing source: a `verify_command` field on scheduled jobs, threaded through the scheduler→NATS→Python→aggregator chain.

## What I already know

* `aggregator.py:75` — `aggregate(verify_command: str | None = None)`. Line 175-176: if provided, `result.verification_passed = await self._verify(verify_command)`.
* `aggregator.py:263` — `_verify(command)`: runs `asyncio.create_subprocess_exec(*command.split())`, 60s timeout, returns bool.
* `orchestrator.py:506` — `await self.aggregator.aggregate(subtask_results, base_files)` — no verify_command passed.
* `JobFile` (`config.rs:46`) — no verify_command field. The `uc.scheduler.yaml` schema doesn't support it.
* `NatsSubmitDispatcher` (#553) publishes `{task_id, description, project_id}` — no verify_command in the payload.
* Python `_handle_submit` (nats_worker.py:1281) reads those 3 fields → `orchestrator.submit_task(description, project_id=, task_id=)`.
* `orchestrator.submit_task` (line 149) — doesn't take verify_command. The aggregator is called later in `_aggregate_results`.

## The chain to wire

1. `JobFile` gains `verify_command: Option<String>` (config field, `uc.scheduler.yaml`).
2. `ScheduledTask` gains a `verify_command: Option<String>` field (or the config value flows to the dispatch).
3. `NatsSubmitDispatcher` includes `verify_command` in the published payload (`{task_id, description, project_id, verify_command}`).
4. Python `_handle_submit` reads `verify_command` → passes to `orchestrator.submit_task(verify_command=)`.
5. `orchestrator.submit_task` stores verify_command on the task → passes to `aggregate(verify_command=)` in `_aggregate_results`.
6. `AggregatedResult.verification_passed` is now populated when verify_command is set.

## Open Questions

* **ScheduledTask field**: add `verify_command: Option<String>` to ScheduledTask (uc-types)? OR thread it separately (the NatsSubmitDispatcher builds the payload from the ScheduledTask)? Recommend: add to ScheduledTask — it's the per-task metadata, natural home.
* **Verification timeout**: `_verify` has a 60s timeout. Build/test commands may take longer. Keep 60s (PRD scope) or make configurable? Recommend: keep 60s for MVP (the existing _verify default).
* **Fallback**: if verify_command is None (no field set), no verification (current behavior — `verification_passed` stays None).

## Requirements (evolving)

* `JobFile.verify_command: Option<String>` (config, `uc.scheduler.yaml`).
* `ScheduledTask.verify_command: Option<String>` (uc-types, threaded from config).
* `NatsSubmitDispatcher` payload includes verify_command (when present).
* Python `_handle_submit` + `orchestrator.submit_task` pass it through.
* `aggregate(verify_command=)` called with it → `verification_passed` populated.
* Fallback: None = no verification (current behavior).

## Acceptance Criteria

* [ ] JobFile has verify_command field
* [ ] ScheduledTask has verify_command field
* [ ] NatsSubmitDispatcher publishes it
* [ ] Python _handle_submit reads it
* [ ] orchestrator.submit_task passes it to aggregate
* [ ] verification_passed populated when verify_command set
* [ ] Fallback (None) = no verification
* [ ] Tests
* [ ] CI green

## Out of Scope

* Configurable timeout (keep 60s)
* Verification result UI (just the field on AggregatedResult)
* RemoveJob/UpdateJob RPCs (separate)

## Technical Notes

* `aggregator.py:75,175,263` — the verify path
* `orchestrator.py:149,506` — submit_task + aggregate call
* `crates/uc-engine/src/scheduler/config.rs:46` — JobFile
* `crates/uc-types/src/scheduler.rs` — ScheduledTask
* `crates/uc-grpc/src/scheduler_dispatch.rs` — NatsSubmitDispatcher (#553)
* `python/ultimate_coders/nats_worker.py:1281` — _handle_submit
* #550 PRD — deferred verify_command
