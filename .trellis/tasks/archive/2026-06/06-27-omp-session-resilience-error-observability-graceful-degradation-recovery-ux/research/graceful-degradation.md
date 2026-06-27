# Research: Graceful Degradation

- **Query**: When gRPC is unavailable, what happens to task submission, memory operations, worker checks, and other operations?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | gRPC client with reconnect + fallback values |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Core orchestrator -- uses bridge for all gRPC ops |
| `packages/uc-orchestrator/src/orchestrator/task-bridge.ts` | LLM tool: uc_task (direct bridge calls) |
| `packages/uc-orchestrator/src/orchestrator/memory-bridge.ts` | LLM tool: uc_memory, uc_search (direct bridge calls) |
| `packages/uc-orchestrator/src/orchestrator/worker-bridge.ts` | LLM tool: uc_worker (direct bridge calls) |
| `packages/uc-orchestrator/src/orchestrator/file-bridge.ts` | LLM tool: uc_file (direct bridge calls) |
| `packages/uc-orchestrator/src/orchestrator/index-bridge.ts` | LLM tool: uc_index (direct bridge calls) |

### Degradation Analysis by Operation

#### 1. Task Submission (`submitTask`)

**Orchestrator path** (`orchestrator.ts:176-233`):
- Creates task locally in `this.tasks` Map immediately (line 188)
- Persists to local JSON via `this.store.save()` (line 193)
- Decomposition uses `runSubprocess` (OMP local agent), NOT gRPC -- works offline
- `syncTaskToGrpc(task)` is fire-and-forget (line 207, 223) -- silently fails if gRPC down
- gRPC bridge's `submitTask()` is NOT called by the orchestrator at all -- the orchestrator uses `upsertTask()` for sync
- **Result**: Task submission works fully offline. Local decomposition + execution proceeds without gRPC.

**LLM tool path** (`task-bridge.ts:34-63`):
- Calls `bridge.submitTask(p.description)` directly
- `GrpcBridge.submitTask()` returns `{ ok: false, error: { kind: "server_unavailable", ... } }` when gRPC is down (line 280-290)
- The tool returns an `isError: true` response with the error message
- **Result**: LLM tool submit FAILS with error when gRPC is unavailable. No local fallback.
- **Gap**: The LLM tool does not fall back to the orchestrator's local task submission. It uses the bridge directly, not the orchestrator.

#### 2. Memory Operations (`readMemory`, `writeMemory`, `searchMemory`)

**Orchestrator path** (`orchestrator.ts`):
- `readMemory`: Not called directly by orchestrator
- `writeMemory`: Called fire-and-forget in executeWaves (lines 460-463, 570-584, 1010-1019, 1180-1184) -- all use `.catch(() => {})`
- `searchMemory`: Not called by orchestrator

**LLM tool path** (`memory-bridge.ts`):
- `readMemory`: `bridge.readMemory()` returns `null` when gRPC unavailable (withReconnect fallback)
- Tool response: `"(no memory found)"` with `useless: true` flag
- **Gap**: Cannot distinguish "key doesn't exist" from "gRPC is down". Agent sees the same `useless: true` response.

- `writeMemory`: `bridge.writeMemory()` returns `false` when gRPC unavailable
- Tool response: `"Write failed"` with `isError: true`
- **Gap**: No local cache/store fallback. Memory writes are lost when gRPC is down.

- `searchMemory`: `bridge.searchMemory()` returns `[]` when gRPC unavailable
- Tool response: `"(no results)"` with `useless: true`
- **Gap**: Same as readMemory -- cannot distinguish empty results from unavailable service.

- `deleteMemory`: `bridge.deleteMemory()` returns `false` when gRPC unavailable
- Tool response: `"Delete failed for key X"`
- **Gap**: No local fallback. Delete silently fails.

#### 3. Worker Checks (`listWorkers`)

**Orchestrator path** (`orchestrator.ts:312-320`):
- `checkWorkerAvailability()` calls `this.bridge.listWorkers()`
- If `!result.available || result.availableCount === 0` -- task fails with "No workers available"
- If gRPC is down, `listWorkers()` returns `{ available: false, workers: [], total: 0, availableCount: 0 }`
- **Result**: When gRPC is unavailable, orchestrator treats it as "no workers available" and fails the task
- **Critical gap**: The orchestrator uses OMP `runSubprocess` for actual subtask execution (line 1046), which runs locally. It does NOT actually need gRPC workers to execute subtasks. The worker check is a false gate that blocks local execution when gRPC is down.

**LLM tool path** (`worker-bridge.ts`):
- Returns `"(worker service unavailable -- gRPC server may be down)"` when `!result.available`
- Properly indicates degraded state to the agent

#### 4. Search Operations (`searchCode`)

**LLM tool path** (`memory-bridge.ts` searchSchema):
- `bridge.searchCode()` returns `[]` when gRPC unavailable
- Tool response: `"(no results)"` with `useless: true`
- **Gap**: Same as memory search -- no distinction between empty results and service unavailability.

#### 5. File Operations (`listDir`, `getFile`)

**LLM tool path** (`file-bridge.ts`):
- Both return empty/null when gRPC unavailable
- No local filesystem fallback
- **Gap**: Cannot read local files when gRPC is down, even though the files are on the same machine.

#### 6. Index Operations (`indexRepo`, `listRepos`, etc.)

**LLM tool path** (`index-bridge.ts`):
- All return `false`/empty/null when gRPC unavailable
- No local fallback
- These operations genuinely require the Rust engine, so no fallback is expected.

### Critical Gaps Summary

| Operation | Has Local Fallback? | Actual Dependency on gRPC | Gap Severity |
|---|---|---|---|
| Task submit (orchestrator) | Yes (local decomposition) | Sync only | None -- works offline |
| Task submit (LLM tool) | No | Direct bridge call | **HIGH** -- fails when gRPC down |
| Memory read | No | Required for TiKV/Qdrant | Medium -- data not local |
| Memory write | No | Required for TiKV/Qdrant | Medium -- data not local |
| Worker check | No (returns false) | Not actually needed for local exec | **HIGH** -- blocks local exec |
| Search | No | Required for Qdrant | Low -- search genuinely needs gRPC |
| File ops | No | Not needed for local files | Medium -- could read local FS |
| Index ops | No | Genuinely requires Rust engine | None -- expected |

### The Worker Availability False Gate

The most significant degradation issue is in `orchestrator.ts:352-364`:

```typescript
let workersAvailable = await this.checkWorkerAvailability();
if (!workersAvailable) {
  await new Promise((r) => setTimeout(r, 5000));
  workersAvailable = await this.checkWorkerAvailability();
}
if (!workersAvailable) {
  task.status = "failed";
  task.error = "No workers available -- all workers offline or overloaded";
  // ...
  break;
}
```

This check gates wave execution. But the actual subtask execution (line 1046) uses `runSubprocess` which runs OMP coding agents locally. It does NOT dispatch to gRPC workers. Therefore:
- When gRPC is down, `listWorkers()` returns `{ available: false }` and the task fails
- But the task could actually execute fine locally via OMP subprocess
- The worker check is conflating "gRPC is down" with "no workers available"

### Related Specs

- `.trellis/spec/backend/error-handling.md` -- Documents the Python-side gRPC fallback mode (fallback_mode="auto") which switches to local engine. The OMP TypeScript orchestrator has no equivalent.
- `.trellis/spec/backend/nats-bridge-spec.md` -- Documents the gRPC fallback pattern at the Python layer.

## Caveats / Not Found

- The LLM tools (uc_task, uc_memory, etc.) use the bridge directly, bypassing the orchestrator. This is by design (the tools provide gRPC-level operations), but it means the orchestrator's local task management is not accessible through LLM tools.
- Whether the worker check should be skipped when using local execution is a design decision. The current code assumes gRPC workers are always needed.
