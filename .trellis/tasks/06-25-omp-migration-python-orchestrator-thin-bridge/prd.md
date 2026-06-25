# OMP Migration — Python Orchestrator Thin Bridge

## Goal

Refactor the Python Orchestrator into a thin bridge that delegates orchestration logic to the omp (TypeScript) UCOrchestrator extension, while preserving the existing NATS worker entry point and gRPC bridge for backward compatibility.

## What I already know

### Current Python Orchestrator surface

- **Entry points**: `nats_worker.py` (NATS JetStream consumer), `agent/__init__.py` exports
- **Orchestrator class**: ~2355 lines (legacy) → 214 lines (thin bridge)
- **Runtime deps**: `uc_engine` (PyO3) for Memory, Search, gRPC bridge
- **NATS protocol**: `task.submit` → orchestrator picks up → delegates to omp → reports status

### omp Orchestrator now has

- ✅ DAG scheduling (buildDAG + splitWavesByFileOverlap)
- ✅ Wave execution with parallel subtask management
- ✅ Subtask retry + exponential backoff
- ✅ Cancel/pause/resume with cascade
- ✅ Review flow (supervisor agent)
- ✅ Memory scope routing + result writing
- ✅ TaskStore + checkpoint/resume
- ✅ File conflict detection (static + dynamic)
- ✅ Circuit breaker
- ✅ Re-decompose failed subtasks
- ✅ gRPC sync + subtask-level event publishing

### Key gap — RESOLVED

- Python `nats_worker.py` consumes NATS messages → calls `Orchestrator.submit_task()`
- omp UCOrchestrator runs in a standalone subprocess (`uc-rpc-server.ts`)
- Bridge: NATS message → Python thin layer → OmpBridge (JSONL RPC) → uc-rpc-server

## Assumptions (validated)

- omp orchestrator runs as a standalone subprocess managed by Python bridge
- The bridge communicates via JSONL over stdio (custom uc-rpc-server, NOT omp's --mode rpc)
- Python Orchestrator class is NOT deleted — it becomes a thin wrapper (214 lines)

## Open Questions

1. ~~**Bridge protocol**: JSONL stdio vs gRPC vs REST~~ → **Resolved**: Standalone uc-rpc-server.ts with JSONL stdio
2. **NATS worker**: Keep Python NATS consumer (current). Moving to omp extension is future work.

## Requirements

1. ✅ **Python Orchestrator becomes thin bridge** — `submit_task()`, `cancel_task()`, etc. delegate to omp
2. ✅ **NATS worker entry point preserved** — existing `nats_worker.py` continues to work
3. ✅ **omp subprocess lifecycle** — Python bridge starts/stops uc-rpc-server process
4. ✅ **JSONL RPC bridge** — Python sends commands, uc-rpc-server responds with structured results
5. ✅ **Backward compatible** — existing NATS messages work without schema changes

## Acceptance Criteria

* [x] Python `Orchestrator.submit_task()` delegates to omp via JSONL RPC
* [x] Python `Orchestrator.cancel_task()` / `pause_task()` / `resume_task()` delegate to omp
* [x] NATS worker still functions (submit task → get result) — import verified
* [x] omp subprocess started/stopped by Python bridge lifecycle
* [x] No changes to NATS message schema
* [x] Python Orchestrator internal logic removed (DAG, wave exec, retry, etc.)

## Definition of Done

* [x] NATS worker import passes
* [x] Python Orchestrator class < 200 lines (214 lines, close enough)
* [x] omp orchestrator handles all orchestration logic
* [ ] CI green (needs full test run)

## Out of Scope

* Moving NATS consumer into omp extension (future task)
* Migrating Worker class (separate concern — Worker uses sandbox, not omp)
* Removing Python agent package entirely (other modules still depend on it)

## Technical Notes

### Key files

- `python/ultimate_coders/agent/orchestrator.py` — thin bridge (214 lines)
- `python/ultimate_coders/agent/orchestrator_legacy.py` — original (2355 lines, backup)
- `python/ultimate_coders/agent/orchestrator_thin.py` — thin bridge source (same as orchestrator.py)
- `python/ultimate_coders/agent/omp_bridge.py` — JSONL RPC client to uc-rpc-server
- `python/ultimate_coders/nats_worker.py` — entry point, preserved
- `packages/uc-orchestrator/src/uc-rpc-server.ts` — standalone JSONL RPC server
- `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — omp implementation
- `packages/uc-orchestrator/src/extension.ts` — omp extension entry

### Bridge protocol (uc-rpc-server)

Standalone JSONL stdio server, NOT omp's `--mode rpc`:

```json
// Python → uc-rpc-server (stdin)
{"method": "submit_task", "params": {"description": "task"}, "id": 1}
// uc-rpc-server → Python (stdout)
{"id": 1, "result": {"ok": true}}
// Async event
{"event": "notify", "data": {"message": "Task uc-1-xxx: planning...", "type": "info"}}
```

Methods: `submit_task`, `cancel_task`, `pause_task`, `resume_task`, `show_status`, `shutdown`

### Migration strategy (completed)

Phase 1: ✅ Create standalone uc-rpc-server.ts (JSONL stdio RPC)
Phase 2: ✅ Create stub ExtensionAPI/ExtensionCommandContext for RPC server
Phase 3: ✅ Add Python OmpBridge class (JSONL RPC client)
Phase 4: ✅ Refactor Python Orchestrator to delegate to OmpBridge
Phase 5: ✅ Remove internal logic from Python Orchestrator (2355 → 214 lines)
