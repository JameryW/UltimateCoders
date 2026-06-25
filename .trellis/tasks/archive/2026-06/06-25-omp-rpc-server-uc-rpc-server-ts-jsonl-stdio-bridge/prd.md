# OMP RPC Server — uc-rpc-server.ts JSONL stdio bridge

## Goal

Create `uc-rpc-server.ts` — a standalone Bun script that the Python `OmpBridge` spawns as a subprocess. It reads JSONL commands from stdin, delegates to `UCOrchestrator`, and writes JSONL responses + events to stdout. This is the missing piece that makes the Python thin bridge functional.

## What I already know

### Python OmpBridge protocol (already implemented)

- Spawns `bun run uc-rpc-server.ts` via `asyncio.create_subprocess_exec`
- Sends: `{"method": "<name>", "params": {...}, "id": <int>}`
- Expects response: `{"id": <int>, "result": {...}}` or `{"id": <int>, "error": "<msg>"}`
- Async events: `{"event": "<type>", "data": {...}}`
- First message on stdout must be `{"event": "ready"}`

### UCOrchestrator methods to expose

| RPC method | Orchestrator call | Return |
|---|---|---|
| `submit_task` | `submitTask(description, ctx)` | `{task_id}` |
| `cancel_task` | `cancelTask(task_id, subtask_id?)` | `{ok: bool}` |
| `pause_task` | `pauseTask(task_id)` | `{ok: bool}` |
| `resume_task` | `resumeTask(task_id)` | `{ok: bool}` |
| `show_status` | `showStatus(task_id?)` | `{status: string}` |
| `get_task` | `tasks.get(task_id)` → serialize | `{task: {...}}` |
| `list_tasks` | `tasks` map → serialize all | `{tasks: [...]}` |
| `shutdown` | graceful exit | `{ok: true}` |

### Constraints

- `tasks` map is `private` on UCOrchestrator — need a public getter
- `submitTask`, `cancelTask`, `pauseTask`, `resumeTask`, `showStatus` require `ExtensionCommandContext` — RPC server has no omp context
- UCOrchestrator constructor requires `ExtensionAPI` — RPC server is standalone

## Assumptions (temporary)

- RPC server runs without real omp ExtensionAPI — use a stub/mock `pi` that provides logger + settings
- `ExtensionCommandContext` can be stubbed (cwd + notify are the only used fields)
- Events from orchestrator (task lifecycle) forwarded as JSONL events on stdout

## Requirements

1. **JSONL stdio server** — read commands from stdin line-by-line, dispatch to UCOrchestrator, write responses to stdout
2. **Ready signal** — emit `{"event": "ready"}` on startup before processing commands
3. **Orchestrator instantiation** — create UCOrchestrator with stub ExtensionAPI + GrpcBridge
4. **Task state access** — add `getTaskState(id)` and `getAllTaskStates()` public getters to UCOrchestrator
5. **Method dispatch** — handle all 8 RPC methods listed above
6. **Event forwarding** — bridge orchestrator state changes to JSONL events on stdout
7. **Graceful shutdown** — `shutdown` method closes orchestrator, drains pending responses, exits

## Acceptance Criteria

* [ ] `bun run uc-rpc-server.ts` starts, emits `{"event": "ready"}`, processes commands
* [ ] `submit_task` → task created, response includes `task_id`
* [ ] `cancel_task` / `pause_task` / `resume_task` → correct boolean response
* [ ] `get_task` → returns full task state with subtasks
* [ ] `list_tasks` → returns array of all tasks
* [ ] `shutdown` → server exits cleanly
* [ ] Python OmpBridge can connect and execute all methods
* [ ] Task state getters added to UCOrchestrator (public)

## Definition of Done

* Unit test for JSONL command dispatch (can use mock stdin/stdout)
* Integration test: Python OmpBridge ↔ uc-rpc-server.ts round-trip
* Lint/typecheck green

## Out of Scope

* gRPC server (already exists separately)
* Authentication/authorization on RPC channel
* Multiple concurrent Python clients (single client assumed)
* omp ExtensionAPI full mock (only stub what RPC server needs)

## Technical Approach

1. Add `getTaskState(id)` and `getAllTaskStates()` getters to UCOrchestrator
2. Create `uc-rpc-server.ts` with:
   - Stub ExtensionAPI (logger → stderr, settings → {workspaceRoot: cwd})
   - Stub ExtensionCommandContext (cwd, notify → no-op)
   - JSONL reader loop on stdin
   - Method dispatch table
   - JSONL writer on stdout for responses + events
3. Wire orchestrator lifecycle events → JSONL event emission

## Technical Notes

### Key files

* `packages/uc-orchestrator/src/uc-rpc-server.ts` — new file
* `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` — add public getters

### Python reference

* `python/ultimate_coders/agent/omp_bridge.py` — client side (already exists on feat/dashboard-v3-observability-phase2 branch)

### omp ExtensionAPI surface (what we need to stub)

```typescript
interface ExtensionAPI {
  pi: { settings: Record<string, unknown> };
  logger: { info(msg: string): void; warn(msg: string): void; error(msg: string): void };
  registerCommand: never;  // not needed
  registerTool: never;     // not needed
  sendMessage: never;      // not needed
}
```
