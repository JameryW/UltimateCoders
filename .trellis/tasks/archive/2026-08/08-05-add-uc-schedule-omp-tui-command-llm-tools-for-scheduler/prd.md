# Add uc_schedule OMP TUI command + LLM tools for scheduler

## Goal

The scheduler RPCs exist (GetSchedulerStatus, TriggerSchedulerJob, AddCronJob, RemoveJob — #556/#562) but the OMP TUI has no `uc_schedule` command and no LLM-callable scheduler tools. The omp-tools-spec (line 305) notes `uc_schedule` is "Phase 2, needs Python RPC bridge for SchedulerService". Wire it — expose scheduler operations to the OMP user (via `/uc schedule`) and the LLM (via `uc_scheduler_*` tools).

## What I already know

* OMP extension registers tools: `registerMemoryTools`, `registerTaskTools`, `registerIndexTools`, `registerFileTools`, `registerWorkerTools` (extension.ts:771-775). No `registerSchedulerTools`.
* The gRPC bridge (`packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts`) has `searchCode`, `submitTask`, `getTask`, etc. No scheduler methods.
* Scheduler RPCs on the Rust gRPC server: `GetSchedulerStatus`, `TriggerSchedulerJob`, `AddCronJob` (#556), `RemoveJob` (#562).
* The `/uc` command dispatch (extension.ts) has subcommands: submit, status, cancel, pause, resume, search, help. No `schedule` subcommand.

## Implementation

1. **`grpc-bridge.ts`**: add scheduler methods — `getSchedulerStatus()`, `triggerSchedulerJob(jobId)`, `addCronJob(request)`, `removeJob(jobId)`. These call the gRPC-Web RPCs (the proto messages already exist).

2. **New `scheduler-bridge.ts`** (mirror `memory-bridge.ts`/`task-bridge.ts` pattern): `registerSchedulerTools(pi, bridge)` — registers LLM-callable tools: `uc_scheduler_status`, `uc_scheduler_trigger`, `uc_scheduler_add_cron`, `uc_scheduler_remove_job`.

3. **`/uc schedule` subcommand** (extension.ts): a new subcommand in the `/uc` dispatch. Sub-actions:
   - `/uc schedule` (no args) → show scheduler status (jobs, night window, execution history)
   - `/uc schedule trigger <job_id>` → trigger a job
   - `/uc schedule add <description> <cron>` → add a cron job (simplified; full AddCronJob fields via flags)
   - `/uc schedule remove <job_id>` → remove a job

4. **Register** `registerSchedulerTools(pi, bridge)` in extension.ts (line ~776).

## Open Questions

* **AddCronJob CLI ergonomics**: the full AddCronJobRequest has 7 fields (description, cron, project_id, night_window_start/end, timezone, enabled). For `/uc schedule add`, use positional args for description + cron, flags/env for the rest (project_id from context, night_window optional). Recommend: `add <description> <cron> [--project <id>] [--night-start HH:MM --night-end HH:MM] [--tz <zone>]`.
* **UI rendering**: scheduler status is a snapshot (jobs list + night window + recent execution history). Render via `ctx.ui.notify()` (toast) like `/uc status`, OR via an overlay (like the task-list overlay)? Recommend: notify toast for MVP (mirrors `/uc status`); overlay is future polish.

## Requirements (evolving)

* `grpc-bridge.ts`: 4 scheduler methods (getSchedulerStatus, triggerSchedulerJob, addCronJob, removeJob).
* `scheduler-bridge.ts`: registerSchedulerTools with 4 LLM tools.
* `extension.ts`: `/uc schedule` subcommand (status/trigger/add/remove).
* Register the tools.
* Tests: selfcheck for the bridge + command parsing.

## Acceptance Criteria

* [ ] grpc-bridge has scheduler methods
* [ ] scheduler-bridge registers LLM tools
* [ ] `/uc schedule` command works (status/trigger/add/remove)
* [ ] Tools registered in extension.ts
* [ ] Selfcheck passes
* [ ] `bun run check` clean
* [ ] CI green

## Out of Scope

* Scheduler overlay UI (toast for MVP)
* UpdateJob RPC (not implemented yet — separate)
* Dashboard SchedulerPanel runtime UI (frontend task)

## Technical Notes

* `packages/uc-orchestrator/src/extension.ts:771-775` — tool registration block
* `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` — the gRPC client
* `packages/uc-orchestrator/src/orchestrator/memory-bridge.ts` — the bridge pattern to mirror
* `packages/uc-orchestrator/src/orchestrator/task-bridge.ts` — the bridge pattern (registerTaskTools)
* `crates/uc-grpc/proto/engine.proto` — scheduler RPCs + messages
* `.trellis/spec/backend/omp-tools-spec.md:305` — the `uc_schedule` Phase 2 note
