# Research: Parallel Step Event Flow to TUI

- **Query**: How do parallel step execution events flow to the TUI, and what changes are needed to visualize multiple concurrent steps?
- **Scope**: internal (mixed: Python worker + Rust gRPC + TypeScript TUI + React dashboard)
- **Date**: 2026-07-13

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/worker.py` (L1035-1384) | Worker event emission: `_emit_step_event`, `_execute_steps` (parallel path), `_run_single_step` |
| `crates/uc-engine/src/events.rs` (L50-72) | `AgentEventType::SubtaskProgress` Rust variant definition |
| `crates/uc-grpc/src/conversions.rs` (L925-1003, L1366+) | Rust conversion: `AgentEventType` → proto `TaskEvent` (data map construction) |
| `crates/uc-grpc/proto/engine.proto` (L336-342) | `TaskEvent` proto: `map<string,string> data = 5` |
| `crates/uc-grpc/src/server.rs` (L2035-2055) | NATS event → `TaskEvent` proto → WatchTask broadcast |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` (L367-386) | TUI ingestion layer 1: WatchTask event → re-emit `subtask_progress` |
| `packages/uc-orchestrator/src/orchestrator/events.ts` (L40-51) | `OrchestratorEvents["subtask_progress"]` TypeScript interface |
| `packages/uc-orchestrator/src/extension.ts` (L142-163) | TUI ingestion layer 2: event → `progressBySubtask.set(subtaskId, info)` |
| `packages/uc-orchestrator/src/ui/progress-widget.ts` (L44-167) | `ProgressWidgetState`, `SubtaskProgressInfo`, `render()` |
| `dashboard/src/hooks/useDashboard.ts` (L493-525) | Dashboard: `mergeProgressEvent` — upserts by `subtask_id` (single step) |
| `dashboard/src/types/dashboard.ts` (L43-70) | `SubtaskSummary` interface with `step_*` fields |
| `dashboard/src/components/panels/TaskDetail.tsx` (L154-185) | `SubtaskProgress` React component (renders single step per subtask) |

---

## Layer 1: Worker Event Emission (worker.py)

### Current Behavior

`_execute_steps` (L1053) groups consecutive steps with the same non-empty `parallel_group` and runs them via `asyncio.gather` (L1182-1189). Each concurrent step calls `_run_single_step` independently.

`_run_single_step` (L1246) emits `subtask_progress` events at these points:
- **skipped** (L1317-1326) — condition false
- **started** (L1329-1337) — before execution
- **retrying** (L1355-1365) — before retry sleep
- **completed/failed** (L1374-1383) — after execution

Every event carries these keys (via `_emit_step_event` → `_publish_event`, L1035-1051):
```
step_index=idx, step_total=total, step_agent=step.agent,
step_status=<status>, step_summary=<summary>,
phase=f"step {idx+1}/{total}: {step.agent}", percent=<calc>,
retry_attempt=<on retrying only>
```

### Parallel Step Interleaving (CONFIRMED)

When N steps run via `asyncio.gather` (L1182-1189), each calls `_run_single_step` which emits events independently. Since `asyncio.gather` runs coroutines concurrently (interleaved at await points), the events ARE interleaved across steps. Example for a parallel group with step 2 (agent=codex) and step 3 (agent=claude-code):
1. step_index=2, step_status="started", step_agent="codex"
2. step_index=3, step_status="started", step_agent="claude-code"
3. (both run concurrently in sandbox)
4. step_index=2, step_status="completed" (or step_index=3 first — nondeterministic)
5. step_index=3, step_status="completed"

Each event DOES carry `step_index` to distinguish which step emitted it. The `subtask_id` is the same for all (they belong to the same subtask).

### The Gap

**`parallel_group` is NOT emitted in the event data.** The `parallel_group` field exists on `WorkflowStep` (used at worker.py L1094, L1139) but is never passed to `_emit_step_event` / `_publish_event`. The event data dict contains: `worker_id`, `phase`, `percent`, `step_index`, `step_total`, `step_agent`, `step_status`, `step_summary`, `retry_attempt`. No `parallel_group` key.

A consumer receiving interleaved events for the same `subtask_id` with different `step_index` values has no way to know whether they are sequential (one after another) or parallel (concurrent). It just sees a sequence of step_index=2, step_index=3, step_index=2, step_index=3 events.

---

## Layer 2: Event Data Dict & Proto Transport

### Current Behavior

The Rust `AgentEventType::SubtaskProgress` variant (`crates/uc-engine/src/events.rs` L56-72) has fields:
```rust
SubtaskProgress {
    task_id, subtask_id, worker_id, phase, percent,
    step_index: Option<u32>,
    step_total: Option<u32>,
    step_agent: Option<String>,
    step_status: Option<String>,
    step_summary: Option<String>,
}
```
No `parallel_group` field.

The conversion to proto (`crates/uc-grpc/src/conversions.rs` L970-1002) builds a `HashMap<String, String>` with keys: `worker_id`, `phase`, `percent`, and conditionally `step_index`, `step_total`, `step_agent`, `step_status`, `step_summary`. No `parallel_group` is inserted.

The proto `TaskEvent` (`engine.proto` L336-342) is:
```proto
message TaskEvent {
    string timestamp = 1;
    string type = 2;
    string task_id = 3;
    optional string subtask_id = 4;
    map<string, string> data = 5;  // event-specific payload as string map
}
```
The `data` is a `map<string,string>` — any new key (like `parallel_group`) can be added without proto changes, as the PRD noted ("data is a map").

### The Gap

To signal parallelism, `parallel_group` must be:
1. **Added to the worker's event data dict** in `_emit_step_event` calls (worker.py) — this is the origin.
2. **Added to `AgentEventType::SubtaskProgress`** Rust variant (events.rs) — optional field.
3. **Inserted into the data HashMap** in conversions.rs L975-1001.

Since data is a `map<string,string>`, no `.proto` change is needed. But the Rust enum variant and conversion must be updated for the Rust-side path (WatchTask broadcast). The Python worker → NATS path publishes the raw JSON dict, so adding `parallel_group` to the Python `_emit_step_event` call is sufficient for the NATS→WatchTask→TUI path (the gRPC server reads `NatsTaskEvent.data` as a `serde_json::Map` and converts to proto `data` map — see server.rs L2035-2055).

---

## Layer 3: TUI Event Ingestion (orchestrator.ts + extension.ts)

### Current Behavior

**Layer 3a — orchestrator.ts** (`handleWatchTaskEvent`, L367-386):
Receives a `TaskEvent` from the gRPC WatchTask stream. If `ev.type !== "subtask_progress"` it returns. Otherwise it extracts fields from `ev.data` (the string map) and re-emits via `this.events.emit("subtask_progress", {...})`. The re-emitted payload includes: `taskId`, `subtaskId`, `workerId`, `phase`, `percent`, `stepIndex`, `stepTotal`, `stepAgent`, `stepStatus`, `stepSummary`. No `parallelGroup` is extracted or re-emitted.

**Layer 3b — extension.ts** (`handleOrchestratorEvent` case `"subtask_progress"`, L142-163):
```typescript
const info: SubtaskProgressInfo = {
    phase: d.phase,
    percent: d.percent,
};
if (d.stepIndex !== undefined) info.stepIndex = d.stepIndex;
if (d.stepTotal !== undefined) info.stepTotal = d.stepTotal;
if (d.stepAgent !== undefined) info.stepAgent = d.stepAgent;
if (d.stepStatus !== undefined) info.stepStatus = d.stepStatus;
if (d.stepSummary !== undefined) info.stepSummary = d.stepSummary;
ps.progressBySubtask.set(d.subtaskId, info);  // ← OVERWRITE
```

The critical line is `ps.progressBySubtask.set(d.subtaskId, info)`. The `progressBySubtask` is `Map<string, SubtaskProgressInfo>` (progress-widget.ts L53). It is keyed by `subtaskId` only. When multiple concurrent steps emit events for the same subtask, each event overwrites the previous entry — only the last-received step's info survives.

### The Gap

The Map key is `subtaskId` (single entry per subtask). For parallel steps, this means:
- Step 2 emits "started" → stored
- Step 3 emits "started" → overwrites step 2's entry
- Step 2 emits "completed" → overwrites step 3's entry
- Step 3 emits "completed" → overwrites step 2's entry

The TUI can never show 2+ concurrent steps for one subtask. The `stepIndex` field IS carried in `SubtaskProgressInfo` but is useless because the Map only holds one entry per subtask.

### Minimal Change for Multi-Step

To show multiple concurrent steps, `progressBySubtask` must become `Map<string, SubtaskProgressInfo[]>` (array per subtask) or `Map<string, Map<number, SubtaskProgressInfo>>` (keyed by step_index). The changes required:

1. **`events.ts`** (L40-51): Add `parallelGroup?: string` to the `subtask_progress` event interface (if signaling parallelism).
2. **`orchestrator.ts`** (L367-386): Extract `parallel_group` from `ev.data` and include in re-emitted payload.
3. **`extension.ts`** (L142-163): Change `set(d.subtaskId, info)` to upsert into an array: find existing entry by `stepIndex`, replace if exists, append if not. Clear entries on step_status "completed"/"failed" (or keep last terminal status).
4. **`progress-widget.ts`** (L53): Change type from `Map<string, SubtaskProgressInfo>` to `Map<string, SubtaskProgressInfo[]>`.

---

## Layer 4: TUI Render (progress-widget.ts)

### Current Behavior

`render()` (L82-145) shows running subtasks (L111-130):
```typescript
const running = task.subtasks.filter((s) => s.status === "running" || s.status === "reviewing");
for (const st of running.slice(0, 3)) {
    const icon = statusIcon(st.status, this.theme);
    const desc = st.description.slice(0, width - 12);
    lines.push(`  ${icon} ${this.theme.fg("dim", st.id)}: ${desc}`);
    const prog = s.progressBySubtask?.get(st.id);  // ← single entry
    if (prog) {
        const agentTag = prog.stepAgent ? this.theme.fg("accent", prog.stepAgent) : "";
        const phaseText = prog.phase ? ... : "";
        const statusTag = prog.stepStatus ? this._stepStatusTag(prog.stepStatus) : "";
        const parts = ["    ", agentTag, phaseText, statusTag].filter(Boolean);
        if (parts.length > 1) lines.push(parts.join(" "));
    }
}
```

It renders ONE indented line per running subtask showing: agent | phase | [status tag]. For a subtask with parallel steps, only the last-received step's info is shown.

`SubtaskProgressInfo` (L57-65):
```typescript
export interface SubtaskProgressInfo {
    phase: string;
    percent: number;
    stepIndex?: number;
    stepTotal?: number;
    stepAgent?: string;
    stepStatus?: string;
    stepSummary?: string;
}
```
It HAS `stepIndex` but the render code doesn't use it (it renders whatever single entry the Map holds).

### The Gap

Even if ingestion stored an array, the render code only reads a single `SubtaskProgressInfo` via `progressBySubtask?.get(st.id)`. It would need to iterate the array and render multiple indented lines.

### Render Options for Parallel Steps

If `progressBySubtask` becomes `Map<string, SubtaskProgressInfo[]>`:

**Option A — Multiple indented lines**: For each step in the array, render an indented line: `    [step 2/4] codex step 2/4: codex [running]`. Shows full detail but consumes vertical space (N lines per parallel subtask).

**Option B — Count + summary**: Render one line: `    3 parallel steps (codex, claude-code, review) ⟳`. Compact but loses per-step status.

**Option C — Hybrid**: If 1 step, show full detail (current behavior). If 2+ steps, show a count + agent list + overall status. Best of both.

---

## Layer 5: Dashboard Ingestion (useDashboard.ts + TaskDetail.tsx)

### Current Behavior

`mergeProgressEvent` (useDashboard.ts L497-524) upserts by `subtask_id` (single entry per subtask), applying `??` fallbacks:
```typescript
return subtasks.map((s) =>
    s.id === sid ? {
        ...s,
        phase: phase ?? s.phase,
        percent: percent ?? s.percent,
        step_agent: step_agent ?? s.step_agent,
        step_status: step_status ?? s.step_status,
        step_index: step_index ?? s.step_index,
        step_total: step_total ?? s.step_total,
        step_summary: step_summary ?? s.step_summary,
    } : s,
);
```
This means each new event for the same subtask overwrites the previous step's fields. Only the last-received step's agent/status/index is shown. Same overwrite problem as the TUI.

`SubtaskSummary` (types/dashboard.ts L43-70) has `step_agent`, `step_status`, `step_index`, `step_total`, `step_summary` — all singular (single step per subtask).

`SubtaskProgress` component (TaskDetail.tsx L154-185) renders a single agent badge + status tag + phase label per subtask.

### The Gap

Same as TUI: the dashboard can only show one step per subtask. For parallel steps, later events overwrite earlier ones. To show multiple, `SubtaskSummary` would need a `steps?: StepProgress[]` array field, and `mergeProgressEvent` would need to upsert into that array by `step_index`.

---

## Key Decision: Multi-Entry vs Single+Indicator

Two approaches to visualize parallel steps:

### Approach A: Multi-Entry (Map<string, SubtaskProgressInfo[]>)

- **Pro**: Full visibility — see each concurrent step's agent, phase, status independently.
- **Con**: More ingestion + render complexity. Changes the `progressBySubtask` type, the `set()` call, the render loop, and the dashboard `SubtaskSummary` type.
- **Worker change**: Must add `parallel_group` to event data (so the consumer knows these are concurrent, not sequential). Without it, the TUI can't distinguish "step 2 completed, then step 3 started" (sequential) from "step 2 and 3 running together" (parallel).

### Approach B: Single Entry + "N parallel steps" Indicator

- **Pro**: Minimal change — keep `Map<string, SubtaskProgressInfo>` as-is, but add a `parallelGroup?: string` and `parallelStepCount?: number` field. The render shows the current step + a "⟳ 2 parallel steps in group 'cr'" indicator.
- **Con**: Loses per-step detail — can't see which parallel step is retrying or failed.
- **Worker change**: Add `parallel_group` and `parallel_step_count` (total steps in the group) to the event data. Still needs the worker.py change.

### Recommendation Flags

- **`parallel_group` is NOT in the event data today** — this is the first gap to fix (worker.py `_emit_step_event` calls must pass `parallel_group=step.parallel_group`).
- **No proto change needed** — `data` is `map<string,string>`, so `parallel_group` flows through as a string key.
- **Rust enum change needed** — `AgentEventType::SubtaskProgress` (events.rs L56-72) needs a `parallel_group: Option<String>` field for the Rust-side WatchTask path, plus the conversion in conversions.rs L975-1001.
- **The `step_index` field already exists** in all layers — it can be used as the secondary key for multi-entry storage.

---

## Related Specs

No spec files found in `.trellis/spec/` directly related to parallel step visualization. The decomposer agent doc (`packages/uc-orchestrator/src/agents/decomposer.md` L193-244) documents the `parallel_group` field and its semantics for the LLM that generates subtask steps.

## Caveats / Not Found

- **No existing PRD** in the task directory (`.trellis/tasks/07-13-.../`) — research is based on the task context provided in the query.
- **NATS publisher path not fully traced** — the Python worker publishes via `nats_publisher.publish_event` (worker.py L577). The NATS message format (`NatsTaskEvent` JSON) was traced via `crates/uc-grpc/src/server.rs` L2035 (deserialization) which reads `data` as `serde_json::Map<String, serde_json::Value>`. Adding `parallel_group` to the Python dict should flow through NATS to the gRPC server to the TUI without proto changes, but the Rust `AgentEventType` enum variant is a separate type that would need the field added for internal Rust consumers.
- **Event ordering**: `asyncio.gather` does NOT guarantee event ordering between concurrent steps. If strict ordering is needed for display, the TUI must sort by `step_index` when rendering.
