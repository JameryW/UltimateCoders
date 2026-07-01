# Journal - JameryW (Part 2)

> Continuation from `journal-1.md` (archived at ~2000 lines)
> Started: 2026-06-25

---



## Session 56: Dashboard v3 Phase 2 — alerts, Prometheus, SQLite persistence

**Date**: 2026-06-25
**Task**: Dashboard v3 Phase 2 — alerts, Prometheus, SQLite persistence
**Branch**: `main`

### Summary

Implemented Dashboard v3 observability phase 2: AlertBar 7 conditions + SQLite alert history + dropdown panel, Prometheus /metrics endpoint (9 gauges/counters/histograms), SQLite trend persistence (MetricsStore, UC_METRICS_RETENTION_DAYS, 1h/6h/24h range selector). 70 new tests. Fixed recent_failed bug (sliding window vs cumulative), check_alerts tuple return, test isolation from real SQLite db. PR #153 merged.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `5ff7a022` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 57: OMP RPC Server — uc-rpc-server.ts JSONL stdio bridge

**Date**: 2026-06-25
**Task**: OMP RPC Server — uc-rpc-server.ts JSONL stdio bridge
**Branch**: `main`

### Summary

Implemented uc-rpc-server.ts: JSONL stdio bridge for Python OmpBridge ↔ TypeScript UCOrchestrator. Added createTask/runTask split for immediate task_id response, public getters (getTaskState, getAllTaskStates), optional ctx on submitTask/resumeTask, stub ExtensionAPI/ExtensionCommandContext. 8 RPC methods: submit_task, cancel_task, pause_task, resume_task, show_status, get_task, list_tasks, shutdown. 10 unit tests / 18 assertions passing. Spec: omp-rpc-server-spec.md.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `7872acd3` | (see git log) |
| `c6fa1905` | (see git log) |
| `19c854bb` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 58: Session check-in — no active task

**Date**: 2026-06-25
**Task**: Session check-in — no active task
**Branch**: `main`

### Summary

No active task this session. One planning task (TUI-OMP unified control path) remains in backlog. No code changes.

### Main Changes

(Add details)

### Git Commits

(No commits - planning session)

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 59: TUI-OMP unified control path — seamless handoff

**Date**: 2026-06-25
**Task**: TUI-OMP unified control path — seamless handoff
**Branch**: `main`

### Summary

Implemented ControlSignalSubscriber: NATS subscription for task_paused/resumed/cancelled events from gRPC server → UCOrchestrator. Polling fallback (2s) when NATS unavailable. CancelTask RPC already existed (proto+Rust+TUI). Fixed GrpcBridge CancelTask routing. Non-blocking subscriber start. Updated spec.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `4c29850a` | (see git log) |
| `d6955e80` | (see git log) |
| `581a38a0` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 60: PR 155: OMP RPC Server + TUI-OMP control path

**Date**: 2026-06-25
**Task**: PR 155: OMP RPC Server + TUI-OMP control path
**Branch**: `main`

### Summary

uc-rpc-server.ts JSONL bridge, ControlSignalSubscriber NATS+polling, Dashboard cancelTask, PR 155, cargo fmt fix

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `7872acd3` | (see git log) |
| `c6fa1905` | (see git log) |
| `4c29850a` | (see git log) |
| `d6955e80` | (see git log) |
| `581a38a0` | (see git log) |
| `dd7fb098` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 61: Add TUI interactive entry in dashboard

**Date**: 2026-06-26
**Task**: Add TUI interactive entry in dashboard
**Branch**: `chore/consolidate-repo-structure`

### Summary

Added WebSocket /ws/tui endpoint to FastAPI backend with PTY management (script(1) wrapping OMP), full-screen xterm.js TUI page at #/tui with auth token + exponential backoff reconnect, hash-based routing in main.tsx, terminal icon in Header, Vite WebSocket proxy. Updated dashboard-spec with WebSocket TUI contract.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `560501ea` | (see git log) |
| `295b1de0` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 62: Batch archive 5 completed tasks

**Date**: 2026-06-26
**Task**: Batch archive 5 completed tasks
**Branch**: `main`

### Summary

Archived 5 completed tasks: replace-tui-frontend-with-omp (#157), add-tui-interactive-entry-in-dashboard (#164), add-worker-status-awareness-to-omp-orchestrator (#165), consolidate-repo-directory-structure (#163), wrap-distributed-coding-agent-capabilities-as-omp-tools (#160). All PRs merged.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `963eba45` | (see git log) |
| `05ccb566` | (see git log) |
| `47f2addc` | (see git log) |
| `a531fcf0` | (see git log) |
| `7291cb1e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 63: Default gRPC + run-cluster.sh + MinimalOrchestrator + README

**Date**: 2026-06-26
**Task**: Default gRPC + run-cluster.sh + MinimalOrchestrator + README
**Branch**: `main`

### Summary

4 PRs merged: (1) run-omp.sh defaults to START_SERVER=true + structured uc_task errors, (2) run-cluster.sh for local distributed deployment, (3) MinimalOrchestrator fixes broken nats_worker/local_worker imports, (4) README updated for new scripts and architecture. All tested end-to-end: NATS worker connects, local_worker ping works, cluster --stop cleans up.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `7291cb1e` | (see git log) |
| `a531fcf0` | (see git log) |
| `47f2addc` | (see git log) |
| `05ccb566` | (see git log) |
| `963eba45` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 64: Fix OMP task interruption + remove local_worker + resolve PR 171 conflicts

**Date**: 2026-06-27
**Task**: Fix OMP task interruption + remove local_worker + resolve PR 171 conflicts
**Branch**: `fix/grpc-bridge-grpc-web-protocol`

### Summary

1) Fix OMP task interruption: subtask timeout 5→10min (configurable), worker check retry, wave-level circuit breaker reset. 2) Remove local_worker entirely (LocalWorkerBridge + local_worker.py + tests, -3886 lines), enforce NATS/Docker-deployed workers only. 3) Resolve PR 171 merge conflicts (run-omp.sh). 4) Fix CI: clippy needless_update + cargo fmt.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `5def4047` | (see git log) |
| `a4d25eee` | (see git log) |
| `1f969098` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 65: OMP session resilience: 18 fixes across 6 layers + v0.2.0 release

**Date**: 2026-06-27
**Task**: OMP session resilience: 18 fixes across 6 layers + v0.2.0 release
**Branch**: `main`

### Summary

Deep analysis of OMP session interruption causes across 6 layers (OMP framework, UC Extension, gRPC server, run-omp.sh, NATS/Python, resource leaks). Implemented 18 fixes: P0 (GrpcBridge reconnect, session_shutdown cleanup, handler dedup, task eviction, restart marker), P2 (shared bridge, NATS reconnect, connection state events, disk cleanup, heartbeat 600→120s), P3 (worker false gate, NATS polling race, 29 error logging), P4 (bulk resync, abort propagation, uc_task fallback), P5 (proto fix: UpdateTask create-if-not-exists). Also: v0.2.0 release, CI workflow with bun test.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `84c7f171` | (see git log) |
| `846bd7f1` | (see git log) |
| `9c96ef94` | (see git log) |
| `52494bcb` | (see git log) |
| `f2b24916` | (see git log) |
| `5fc79042` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 66: Add DispatchMode to Subtask for forced-remote dispatch routing

**Date**: 2026-06-27
**Task**: Add DispatchMode to Subtask for forced-remote dispatch routing
**Branch**: `main`

### Summary

Added DispatchMode enum (Local/Remote/PreferRemote) to Subtask across Rust/Python/TypeScript. Remote mode retries NATS dispatch 3 times then marks Failed. PreferRemote is default (backward compat). Updated publish_ready_subtasks and dispatch_ready_subtasks with routing logic. Updated NATS bridge spec.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `58a83a7f` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 67: Add worker capability matching + OMP UI dispatch_mode display

**Date**: 2026-06-28
**Task**: Add worker capability matching + OMP UI dispatch_mode display
**Branch**: `main`

### Summary

Two features: (1) OMP SubtaskTreeOverlay shows dispatch_mode in expanded details (hidden for default prefer_remote). (2) Subtask.required_capabilities for worker capability matching — workers check ALL caps match before accepting, NACK on mismatch. select_next_subtask supports optional capability filtering. Three-layer type sync (Rust/Python/TypeScript).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `bd73f1a3` | (see git log) |
| `85c9ef57` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 68: Enhance sandbox worker: skill/MCP/tool customization

**Date**: 2026-06-28
**Task**: Enhance sandbox worker: skill/MCP/tool customization
**Branch**: `main`

### Summary

Added tool/skill/mcp customization to sandbox workers. SandboxConfig gains 7 new fields. Subtask carries agent_config dict. ClaudeCodeAdapter generates CLI flags. Worker derives capabilities from config. 275 tests pass (27 new). PR #182.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `e18f2b36` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 69: Sync Rust SandboxConfig with Python agent customization

**Date**: 2026-06-28
**Task**: Sync Rust SandboxConfig with Python agent customization
**Branch**: `main`

### Summary

Synced Rust SandboxConfig with Python-side agent customization fields (tools, allowed_tools, disallowed_tools, mcp_configs, append_system_prompt, agent_name, agents_json). Updated ClaudeCodeAgent.build_request to generate CLI flags. Updated Python to_engine_config to serialize new fields. Updated test_config helpers across all sandbox modules. 329 Rust + 77 Python tests pass. PR #183.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `4ae40848` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 70: Thread agent_config through NATS/gRPC dispatch pipeline

**Date**: 2026-06-29
**Task**: Thread agent_config through NATS/gRPC dispatch pipeline
**Branch**: `main`

### Summary

Threaded agent_config through the full distributed dispatch pipeline. Python _dispatch_remote/_handle_subtask_execute now include/extract agent_config. Rust NatsSubtaskExecute, uc_types::Subtask, and SubtaskProto all gained agent_config_json field. Python Orchestrator.submit_task accepts agent_config. 83 Python + 329 Rust tests pass. PR #184.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `b05f928e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 71: Sandbox worker retry, progress, profiles

**Date**: 2026-06-29
**Task**: Sandbox worker retry, progress, profiles
**Branch**: `feat/sandbox-worker-retry-progress-profiles`

### Summary

Enhanced sandbox worker: auto-retry (3x + backoff), subtask_progress events (preparing→executing→validating→finalizing), expanded AGENT_PROFILES (8) and SUBTASK_TEMPLATES (7), fixed TiKV integration test flakiness

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `03141773` | (see git log) |
| `ee77acc1` | (see git log) |
| `5bd1b624` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 72: Cross-repo search & memory sharing

**Date**: 2026-06-29
**Task**: Cross-repo search & memory sharing
**Branch**: `feat/sandbox-worker-retry-progress-profiles`

### Summary

Completed cross-repo code retrieval and memory sharing: Worker gRPC Engine routing with fallback, SearchQuery.in_all_repos(), auto search context injection into subtasks, Worker read/write_shared_memory() for project-scoped memory, Subtask.project_id + NatsSubtaskExecute.project_id cross-layer propagation, 9 new unit tests, cross-repo-search-spec.md code-spec

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `32317493` | (see git log) |
| `ca07b0a0` | (see git log) |
| `00bf2087` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 73: Cross-repo search deep integration

**Date**: 2026-06-29
**Task**: Cross-repo search deep integration
**Branch**: `feat/sandbox-worker-retry-progress-profiles`

### Summary

Deep integration: agent_config_json propagation, projectId persistence round-trip, Engine MCP Server, Orchestrator project_id, 3 integration tests, spec updates

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `db81fa39` | (see git log) |
| `4b4df769` | (see git log) |
| `d742ef2e` | (see git log) |
| `748f2733` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

## Session 74: Cross-repo search + shared-memory refinement (PR #190 polish)

**Date**: 2026-06-29
**Task**: cross-repo-search-perf-tui
**Branch**: `feat/cross-repo-search-memory-refinement`

### Summary

6-loop automated refinement pass on PR #190. Fixed a Python 3.9 CI regression, exposed `delete_shared_memory`, made write/delete failure semantics symmetric, hardened the receive-side handler against non-dict JSON, and fixed a fire-and-forget task GC trap. Every fix has a test that verified the pre-fix bug.

### Main Changes

- **Py3.9 fix** — `NatsWorker._dispatch_event` lazy-constructed in `start()`; `asyncio.Event()` binds a loop at ctor on ≤3.9 and raised `RuntimeError` in bare-`NatsWorker()` tests. `event-pipeline-spec.md` example corrected.
- **`delete_shared_memory`** — newly exposed; routes to `engine.delete_memory`, broadcasts `uc.memory.changed` (action='delete'). Receive side already invalidates on any action.
- **Write/delete symmetry** — `write_shared_memory` now catches engine exceptions → returns None → skips broadcast (was letting them bubble, could crash a subtask).
- **Receive-side guard** — `_handle_memory_changed` adds `isinstance(data, dict)`; valid-JSON-non-object payloads (`b"123"`) crashed on `data.get()`.
- **Task GC guard** — broadcast tasks held in `Worker._bg_tasks` with `done_callback`; extracted `_broadcast_memory_changed()` helper. asyncio only weakly refs tasks → unreferenced `create_task()` could be GC'd before running.

### Git Commits

| Hash | Message |
|------|---------|
| `669fc1fd` | fix(worker): construct dispatch Event lazily for Python 3.9 |
| `ba32428d` | docs(spec): asyncio primitives must be lazy-constructed for Python 3.9 |
| `f25c4b79` | feat(worker): expose delete_shared_memory with NATS delete broadcast |
| `5ed8e6f9` | fix(worker): make write_shared_memory non-fatal + skip broadcast on failure |
| `f7302a89` | fix(worker): guard _handle_memory_changed against non-dict JSON payload |
| `2d63e0f5` | fix(worker): hold fire-and-forget broadcast tasks to prevent GC |
| `8a10a525` | test(worker): construct asyncio.Event inside the loop for Py3.9 |

### Testing

- [OK] 184 passed (test_sandbox.py + test_async_engine.py), no regressions
- [OK] CI: ruff / Python 3.9 / Python 3.12 / dashboard all pass; bun test queued (runner scarcity)

### Status

[OK] **Completed** — PR #190 contract-complete: read/write/delete symmetric, send/receive robust, concurrency-safe. Mergeable. Spec + PR body updated.

### Next Steps

- Merge decision is the user's (4 gates green; bun queue is known infra constraint)
- Follow-up (separate PR): `nats_worker.py` `_handle_submit` `create_task(_execute_subtasks)` unreferenced — same GC trap, pre-existing, out of scope here


## Session 74: Fix 503 retry + worker fs-mcp capability tags

**Date**: 2026-07-01
**Task**: Fix 503 retry + worker fs-mcp capability tags
**Branch**: `main`

### Summary

Two PRs merged. #199: llm.py retry path now treats 503/server_error/'system is busy'/'try again later' as transient (was only 429/529), via shared _is_transient_api_error() on both Anthropic + litellm paths — a single transient 503 no longer fails a subtask. #200: worker tool capabilities completed — new fs_mcp.py (uc-fs MCP: read_file/write_file/edit_file, workspace path-isolated), _derive_capabilities() now emits lsp (reusing codegraph as LSP backend, not a new uc-lsp server), file-edit (from uc-fs), opt-in browser/debug env flags; new file-edit AGENT_PROFILE; Rust WorkerRegistry unchanged (string-passed AND-subset). 20 new tests, 460 Python + 17 Rust worker tests green.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `8899ec59` | (see git log) |
| `d3ca50d2` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 75: Friendly LLM error display (transient/permanent classification)

**Date**: 2026-07-01
**Task**: Friendly LLM error display (transient/permanent classification)
**Branch**: `main`

### Summary

PR #201 merged. Worker LLM errors now display friendly structured messages: llm.py adds _classify_llm_error + LLMRetryExhaustedError (carries kind/retry_count/root-cause); worker.py _build_friendly_error produces 'LLM 瞬时错误（已重试 N 次）: <root>' / 'LLM 永久错误: <root>' and sets SubtaskResult.error (fixes always-empty bug) on all 3 failure paths; TS error-format.ts classifyError+formatErrorForDisplay with smart root-cause truncation applied to 4 UI render sites + progress-widget now shows first failed error. 23 Python + 17 TS tests. 483 Python + 93 TS tests green. Rust/proto schema unchanged (string passthrough).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `944a535c` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 76: Add --no-spawn flag (UC_NO_SPAWN hard + soft constraint)

**Date**: 2026-07-01
**Task**: Add --no-spawn flag (UC_NO_SPAWN hard + soft constraint)
**Branch**: `main`

### Summary

PR #202 merged. --no-spawn flag disables subtask spawning: Path B (UC uc_task tool, /uc submit, submit_task RPC) HARD-blocked via UC_NO_SPAWN env returning friendly errors at all 3 entry points with shared isSpawnDisabled() helper; Path A (OMP task tool, vendor) SOFT-constrained — run-omp.sh --no-spawn sets UC_NO_SPAWN=1 + advises task.disabledAgents in ~/.omp/agent/config.yml (OMP CLI doesn't accept --spawns flag, SDK options live in vendor submodule, can't hard-disable without fork). ADR documents the constraint. 5 new TS tests, 98/98 bun test green.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `05e6912b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 77: Archive 4 completed deploy tasks (B phase)

**Date**: 2026-07-01
**Task**: Archive 4 completed deploy tasks (B phase)
**Branch**: `main`

### Summary

B phase: verified+archived 4 deploy tasks that were implemented but never archived. separate-deploy-scripts (PR#197/#198), gateway (compose.gateway.yml standalone+in-memory fallback+env injection, compose config valid), worker (PR#193 3 phases: external git clone/push + MergeArbiter + DistributedConflictDetector advisory, 131 tests pass), worker-omp (ScaleWorkers proto RPC+Rust handler+TS client+uc_worker scale/deregister, 5 tests pass). All AC met, no new code — done-but-unarchived remnants. main synced via rebase.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `05e6912b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 78: uc-lsp real-time LSP MCP server (multilspy)

**Date**: 2026-07-01
**Task**: uc-lsp real-time LSP MCP server (multilspy)
**Branch**: `main`

### Summary

PR #203 merged. uc-lsp MCP server replaces codegraph worktree-lag ceiling for real-time symbol ops: lsp_mcp.py exposes 5 LSP tools (go_to_definition/find_references/hover/document_symbols/workspace_symbol) via multilspy, force-closes stale open_file_buffers so each query reads fresh disk content (real-time guarantee), 1-based->0-based conversion, workspace path isolation, graceful degradation when multilspy absent. Python-only MVP (jedi), other langs hint to codegraph/read_file. Optional [lsp] dep extra, Dockerfile precache. Coexists with codegraph (cross-repo/historical). 25 tests, 508 full suite green. trellis-check caught+fixed 2 real bugs (Dockerfile precache no-op, _sync_file dead code).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `0a20d579` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
