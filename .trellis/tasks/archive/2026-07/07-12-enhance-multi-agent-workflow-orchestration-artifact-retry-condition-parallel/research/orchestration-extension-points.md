# Research: Multi-Agent Workflow Orchestration Extension Points

- **Query**: Research the full multi-agent workflow orchestration data flow to prepare for 4 enhancements: (1) structured artifact passing between steps, (2) step-level retry on failure, (3) conditional branch/skip, (4) parallel step execution within a subtask.
- **Scope**: internal
- **Date**: 2026-07-12

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/types.py` | Python `WorkflowStep` (L158), `Subtask` (L83), `SubtaskResult` (L66), `AgentEvent` (L399) |
| `python/ultimate_coders/agent/worker.py` | `_execute_steps` (L1051), `_render_step_prompt` (L1181), `_emit_step_event` (L1033), `execute_subtask` (L592) |
| `python/ultimate_coders/agent/sandbox.py` | `AgentOutput` (L140), `TokenUsage` (L153), `SandboxManager.execute` (L239), adapters (L572+) |
| `python/ultimate_coders/nats_worker.py` | `_handle_subtask_execute` (L1431), `_dispatch_remote` (L1274) — **steps missing from Python dispatch** |
| `python/ultimate_coders/agent/workspace.py` | `WorkspaceManager.acquire` (L178) — single worktree per subtask_id |
| `crates/uc-types/src/agent.rs` | Rust `WorkflowStep` (L124), `Subtask` (L77), `AgentEvent` (L205) |
| `crates/uc-engine/src/events.rs` | `AgentEventType::SubtaskProgress` (L56) with step_index/step_total/step_agent/step_status/step_summary fields |
| `crates/uc-grpc/proto/engine.proto` | `WorkflowStepProto` (L383), `SubtaskProto` (L355), `TaskEvent` (L336) |
| `crates/uc-grpc/src/conversions.rs` | `step_to_proto` (L896), `step_from_proto` (L906), `SubtaskProgress` event mapping (L955) |
| `crates/uc-grpc/src/server.rs` | `NatsSubtaskExecute` (L130) with `steps: Vec<WorkflowStep>` (L162) |
| `packages/uc-orchestrator/src/orchestrator/scheduler.ts` | `WorkflowStepDef` (L17), `SubtaskDef` (L24), `buildDAG` (L64), `splitWavesByFileOverlap` (L188), `CircuitBreaker` (L335) |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | Proto serialization: SubtaskDef→proto (L440), parseTaskFromProto (L774) |
| `packages/uc-orchestrator/src/agents/decomposer.md` | LLM prompt + output schema for `steps[]` (L18-27) |

---

### 1. AgentOutput Structure (the unit each step produces)

**File**: `python/ultimate_coders/agent/sandbox.py:140-149`

```python
@dataclass
class AgentOutput:
    summary: str = ""
    file_changes: list[FileChange] = field(default_factory=list)
    token_usage: TokenUsage | None = None
    success: bool = True
    stderr_tail: str = ""  # last ~10 lines of stderr
    tool_calls: list[str] = field(default_factory=list)
```

`TokenUsage` (L153): `input_tokens`, `output_tokens`, `total_cost_usd`.

`FileChange` (`types.py:49`): `file_path`, `change_type` (ChangeType enum), `diff`.

**Key insight for enhancement (1)**: `AgentOutput` is already a structured object inside `_execute_steps`. The limitation is that only `summary` and `file_changes` are threaded forward via `_render_step_prompt` string interpolation. The full `AgentOutput` (including `tool_calls`, `token_usage`, `stderr_tail`) is NOT passed to the next step. To enable structured artifact passing, `_render_step_prompt` would need to accept the full `list[AgentOutput]` and expose richer template variables (e.g. `{{prev.tool_calls}}`, `{{prev.stderr}}`, or pass the entire AgentOutput as a JSON blob to the agent prompt).

---

### 2. `_render_step_prompt` — Current Artifact Passing

**File**: `python/ultimate_coders/agent/worker.py:1181-1227`

Currently supports these template variables:
- `{{prev_summary}}` — previous step's `AgentOutput.summary`
- `{{prev_files}}` — previous step's modified file paths (one per line)
- `{{stepN.summary}}` / `{{stepN.files}}` — any prior step by index
- `{{context}}` — subtask-level context_block
- `{{file_constraints}}` — comma-joined file_constraints

The rendering is simple `str.replace()` calls. The `step_outputs: list[AgentOutput]` list is available in the method, so passing richer data is a matter of adding more template variables or serializing the full AgentOutput to JSON.

**What changes for structured artifacts**: Either (a) add more `{{prev.*}}` template variables mapping to AgentOutput fields, or (b) inject a JSON blob of prior AgentOutputs into the prompt context. Option (b) is more future-proof — the agent can parse structured data. The `_render_step_prompt` method signature already accepts `step_outputs: list[AgentOutput]`, so no signature change needed for option (b).

---

### 3. Rust Proto `WorkflowStepProto`

**File**: `crates/uc-grpc/proto/engine.proto:383-394`

```proto
message WorkflowStepProto {
    string agent = 1;
    string prompt = 2;
    optional string agent_config_json = 3;
    optional bool abort_on_failure = 4;
}
```

**Proto→TS→Python→Rust field flow**:

1. **Proto** (`engine.proto:383`): `agent`, `prompt`, `agent_config_json`, `abort_on_failure`
2. **Rust domain** (`uc-types/src/agent.rs:124`): `WorkflowStep` struct with `agent`, `prompt`, `agent_config_json: Option<String>`, `abort_on_failure: bool` (serde default_true)
3. **Rust proto conversion** (`conversions.rs:896-913`): `step_to_proto` / `step_from_proto` map field-by-field
4. **Rust NATS** (`server.rs:130`): `NatsSubtaskExecute` carries `steps: Vec<uc_types::WorkflowStep>` (L162), serialized via `serde_json` (snake_case keys)
5. **TS** (`scheduler.ts:17`): `WorkflowStepDef` with `agent`, `prompt`, `agent_config_json?` (string), `abort_on_failure?`
6. **TS proto mapping** (`grpc-bridge.ts:452-457`): maps snake_case SubtaskDef to camelCase proto fields (`agentConfigJson`, `abortOnFailure`)
7. **TS parse from proto** (`grpc-bridge.ts:787,806`): **only maps `agent` and `prompt`** — `agent_config_json` and `abort_on_failure` are DROPPED on the return path. This is a lossy parse.
8. **Python** (`types.py:158`): `WorkflowStep` with `agent`, `prompt`, `agent_config` (dict, NOT string), `abort_on_failure` (bool). `_resolve_agent_config_field` (L126) handles the `agent_config_json` vs `agent_config` key+type mismatch.
9. **Python NATS dispatch** (`nats_worker.py:1295`): **STEPS NOT SERIALIZED** — `_dispatch_remote` JSON payload omits `steps` entirely. The Rust NatsSubtaskExecute DOES include steps (server.rs:1747), so Python-dispatched remote subtasks lose their workflow chain. **Pre-existing bug**.

**For adding fields (retry/condition/parallel)**: Adding proto fields 5, 6, 7 to `WorkflowStepProto` requires:
- `engine.proto` — add fields
- `conversions.rs` — update `step_to_proto` / `step_from_proto`
- `uc-types/src/agent.rs` — add fields to `WorkflowStep` struct + serde defaults
- `scheduler.ts` — add fields to `WorkflowStepDef`
- `grpc-bridge.ts` — update both serialize (L452) and parse (L787) paths
- `types.py` — add fields to `WorkflowStep` + `to_dict`/`from_dict`
- `decomposer.md` — update output schema + prompt
- `nats_worker.py:1295` — **FIX** `_dispatch_remote` to include `steps` (pre-existing bug)

**Backward compat**: All new fields must be `optional` in proto and have serde defaults in Rust/Python. The existing `subtask_without_steps_field_deserializes_empty` test (agent.rs:305) proves the pattern works.

---

### 4. Rust `WorkflowStep` (uc-types)

**File**: `crates/uc-types/src/agent.rs:123-140`

```rust
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct WorkflowStep {
    pub agent: String,
    pub prompt: String,
    #[serde(default)]
    pub agent_config_json: Option<String>,
    #[serde(default = "default_true")]
    pub abort_on_failure: bool,
}
```

Serializes via `serde_json` (used in `NatsSubtaskExecute` at server.rs:162) and via proto (`step_to_proto`/`step_from_proto`). The `#[serde(default)]` on all non-required fields means old payloads without new fields deserialize fine — this is the backward-compat mechanism for adding `retry`, `condition`, `parallel` fields.

`Subtask::steps` (agent.rs:115): `#[serde(default)] pub steps: Vec<WorkflowStep>` — old subtasks without steps get empty vec.

---

### 5. AgentEvent / Step Event Emission

**File**: `python/ultimate_coders/agent/worker.py:1033-1049` (`_emit_step_event`)

Emits `subtask_progress` events with:
- `phase` (e.g. `"step 1/3: claude-code"`)
- `percent`
- `step_index`, `step_total`, `step_agent`, `step_status` ("started"/"completed"/"failed")
- `step_summary` (truncated to 200 chars)

**Rust event schema** (`uc-engine/src/events.rs:56-71`): `AgentEventType::SubtaskProgress` has dedicated fields for `step_index: Option<u32>`, `step_total: Option<u32>`, `step_agent: Option<String>`, `step_status: Option<String>`, `step_summary: Option<String>`. These are already serialized through `conversions.rs:955-994` into the proto `TaskEvent.data` map.

**Proto TaskEvent** (`engine.proto:336-342`): `map<string, string> data` — it's a generic string map, so new step fields (retry_attempt, parallel_group) can be added to the data dict without proto changes. The Rust `conversions.rs` would need new `SubtaskProgress` fields to be strongly typed, OR they can pass through as raw data map entries.

**For retry**: Emit a `subtask_progress` event with `step_status="retrying"` and a `retry_attempt` field in the data dict. No proto change needed (data is a map). If strongly typed in `AgentEventType::SubtaskProgress`, add `retry_attempt: Option<u32>` field.

**For parallel**: Multiple concurrent step events would have the same `step_index` but different `step_agent`. The event schema already supports this (step_index is per-event). However, the TUI/dashboard would need to handle multiple concurrent steps for the same subtask — currently it assumes sequential.

---

### 6. Subtask Dispatch + Worker Acquire

**Flow** (NATS dispatch → worker execution):

1. **Rust server** (`server.rs:1727`): `NatsSubtaskExecute` serialized with `steps: st.steps.clone()` → published to `uc.subtask.execute`
2. **Python worker** (`nats_worker.py:1431`): `_handle_subtask_execute` parses JSON, reconstructs `Subtask` with `steps=[WorkflowStep.from_dict(s) for s in data.get("steps", [])]` (L1512)
3. **Worker** (`worker.py:592`): `execute_subtask` acquires workspace (L649), calls `_execute_in_sandbox` (L935)
4. **Sandbox** (`worker.py:992`): if `subtask.steps` non-empty, calls `_execute_steps` (L1051)
5. **Steps** (`worker.py:1077`): sequential `for idx, step in enumerate(subtask.steps)` loop

**Python dispatch gap** (`nats_worker.py:1295`): `_dispatch_remote` does NOT include `steps` in the JSON. Remote workers dispatched from Python receive empty steps. Rust-dispatched subtasks (server.rs:1747) DO include steps.

**Concurrency model for parallel steps**: The worker uses `asyncio` throughout. `_execute_steps` is an `async def` that runs steps in a sequential `for` loop. To run steps concurrently, replace the `for` loop with `asyncio.gather()` for steps in the same parallel group. The `SandboxManager.execute` is async and returns `AgentOutput`. The worker already runs multiple subtasks concurrently via `asyncio.gather` in `_execute_subtasks` (nats_worker.py:1252), so the asyncio infrastructure exists.

**KEY BLOCKER — worktree conflict**: `WorkspaceManager.acquire(subtask_id)` (workspace.py:178) creates ONE git worktree per subtask. All steps in a subtask share the same `working_dir` (passed to `_execute_steps` at worker.py:994). If parallel steps both write to the same worktree directory, they will conflict — concurrent file edits to the same git working tree will corrupt each other. This is a **hard blocker** for parallel step execution within a subtask.

---

### 7. Decomposer Schema Constraints

**File**: `packages/uc-orchestrator/src/agents/decomposer.md:18-27`

The frontmatter output schema for steps:
```yaml
steps:
  type: array
  items:
    type: object
    properties:
      agent: { type: string, enum: ["claude-code", "codex"] }
      prompt: { type: string }
      abort_on_failure: { type: boolean, default: true }
```

Adding `retry`, `condition`, `parallel` fields means updating this YAML schema and the prompt instructions.

**Validation/parsing**: `WorkflowStep.from_dict` (`types.py:186`) uses `data.get()` with defaults — no strict validation. Adding new fields is a matter of adding `data.get("retry", default)` lines. The decomposer output is parsed by `parse_decomposition_output` (sandbox.py:671) which returns raw dicts — validation happens downstream in `WorkflowStep.from_dict`.

**TS validation**: `scheduler.ts` `WorkflowStepDef` (L17) is a TypeScript interface — no runtime validation. The decomposer JSON is trusted as-is.

---

### 8. Scheduler Wave Logic

**File**: `packages/uc-orchestrator/src/orchestrator/scheduler.ts`

- `buildDAG` (L64): Kahn's algorithm, topological sort by `dependsOn` → `DAGWave[]` (waves of parallel subtasks)
- `splitWavesByFileOverlap` (L188): Greedy graph coloring — subtasks sharing files get split into sub-waves
- `CircuitBreaker` (L335): Threshold=3 failures, resetMs=30000. Prevents cascading failures at the subtask level.
- `FileIntentTracker` (L256): Runtime file conflict tracking between concurrent subtasks

**Parallel steps vs parallel subtasks**: These are different concerns:
- **Parallel subtasks** (across waves): handled by `buildDAG`/`splitWavesByFileOverlap` — each subtask gets its own worktree
- **Parallel steps** (within ONE subtask): all steps share ONE worktree (workspace.py:178 acquires per subtask_id). Parallel steps writing to the same worktree = file conflict.

The CircuitBreaker operates at the subtask level, not the step level. Step-level retry would need its own circuit breaker or retry counter.

---

## Enhancement Feasibility Analysis

### (1) Structured Artifact Passing

**Feasibility: HIGH (straightforward)**

**Files to change**:
- `worker.py:1181` `_render_step_prompt` — add richer template variables or JSON injection
- `decomposer.md` — document new template variables in the prompt

**Backward compat**: Existing `{{prev_summary}}`/`{{prev_files}}` continue to work. New variables are additive.

**Minimal schema additions**: None — `AgentOutput` already has all the fields. The change is in how `_render_step_prompt` exposes them.

### (2) Step-Level Retry on Failure

**Feasibility: HIGH (medium complexity)**

**Files to change**:
- `engine.proto:383` `WorkflowStepProto` — add `optional uint32 retry_count = 5;` and `optional uint32 retry_delay_ms = 6;`
- `uc-types/src/agent.rs:124` `WorkflowStep` — add `retry_count: u32`, `retry_delay_ms: u64`
- `conversions.rs:896` — update `step_to_proto`/`step_from_proto`
- `scheduler.ts:17` `WorkflowStepDef` — add `retry_count?`, `retry_delay_ms?`
- `grpc-bridge.ts:452,787` — update both serialize and parse paths
- `types.py:158` `WorkflowStep` — add `retry_count: int = 0`, `retry_delay_ms: int = 0` + `to_dict`/`from_dict`
- `worker.py:1051` `_execute_steps` — add retry loop around `self._sandbox_manager.execute()`
- `decomposer.md` — add `retry_count` to schema + prompt instructions
- `nats_worker.py:1295` — FIX `_dispatch_remote` to include `steps` (pre-existing bug)

**Backward compat**: `retry_count = 0` means no retry (current behavior). `serde(default)` ensures old payloads work.

**Event emission**: Emit `subtask_progress` with `step_status="retrying"` and `retry_attempt` in data dict. No proto change needed for events (data is a `map<string,string>`).

### (3) Conditional Branch/Skip

**Feasibility: MEDIUM (requires design decision on condition language)**

**Files to change**:
- `engine.proto:383` — add `optional string condition = 5;` (string expression)
- `uc-types/src/agent.rs:124` — add `condition: Option<String>`
- `conversions.rs:896` — update step_to_proto/step_from_proto
- `scheduler.ts:17` — add `condition?: string`
- `grpc-bridge.ts:452,787` — update both paths
- `types.py:158` — add `condition: str = ""` + `to_dict`/`from_dict`
- `worker.py:1051` `_execute_steps` — evaluate condition before running step; skip if false
- `decomposer.md` — add `condition` to schema + prompt instructions
- `nats_worker.py:1295` — FIX `_dispatch_remote` to include `steps`

**Design decision**: What condition language? Options:
- Simple `{{prev.success}}` boolean (checks if previous step succeeded)
- Jinja2-like expression evaluated against prior step outputs
- A simple `condition: "prev.success == true"` string parsed by the worker

**Backward compat**: Empty condition string = always run (current behavior).

### (4) Parallel Step Execution Within a Subtask

**Feasibility: LOW — BLOCKED by worktree conflict**

**BLOCKER**: `WorkspaceManager.acquire(subtask_id)` (workspace.py:178) creates ONE git worktree per subtask. All steps share `working_dir` (worker.py:994). Parallel steps writing to the same git working tree will corrupt each other — concurrent file edits, `git add`/`git commit` race conditions, and `get_changes_and_reset` (sandbox.py:311) will capture a mix of both steps' changes.

**Possible mitigations** (NOT researched in depth — would need design work):
1. **One worktree per step** — `WorkspaceManager.acquire` would need to accept a step_id, not just subtask_id. Each parallel step gets its own worktree, changes merged after. Major refactor.
2. **Read-only parallel steps** — Only parallelize steps that don't modify files (e.g., review/analysis steps). Check `step.agent_config` for `disallowed_tools: ["Edit","Write"]`. Safer but limits the use case.
3. **Lock-based serialization** — Steps declare file intents (like `FileIntentTracker` in scheduler.ts:256), and parallel execution only proceeds for non-overlapping files. Complex.
4. **Sequential fallback** — If parallel steps conflict, fall back to sequential. This defeats the purpose.

**Files to change (if worktree conflict is resolved)**:
- `engine.proto:383` — add `optional string parallel_group = 5;` (steps with same group run in parallel)
- `uc-types/src/agent.rs:124` — add `parallel_group: Option<String>`
- `conversions.rs:896` — update step conversion
- `scheduler.ts:17` — add `parallel_group?`
- `grpc-bridge.ts:452,787` — update both paths
- `types.py:158` — add `parallel_group: str = ""` + `to_dict`/`from_dict`
- `worker.py:1051` `_execute_steps` — replace `for` loop with `asyncio.gather` for steps in same parallel_group
- `decomposer.md` — add `parallel_group` to schema + prompt instructions
- `nats_worker.py:1295` — FIX `_dispatch_remote` to include `steps`

**Backward compat**: Empty `parallel_group` = sequential (current behavior). Only steps with the same non-empty `parallel_group` run concurrently.

**Event emission**: Multiple concurrent step events with the same `step_index` but different `step_agent`. The TUI/dashboard would need to handle concurrent step displays (currently assumes sequential).

---

## Caveats / Not Found

### Pre-existing Bug: Python `_dispatch_remote` omits `steps`

**File**: `python/ultimate_coders/nats_worker.py:1295-1307`

The `_dispatch_remote` method serializes the subtask to JSON for NATS dispatch but does NOT include the `steps` field. The Rust server's `NatsSubtaskExecute` (server.rs:1747) DOES include `steps: st.steps.clone()`. This means:
- Subtasks dispatched from the Rust gRPC server → remote Python workers: steps preserved
- Subtasks dispatched from the Python orchestrator → remote Python workers: **steps silently lost**

Any enhancement to steps (retry/condition/parallel) will be invisible to Python-dispatched remote subtasks until this is fixed. The fix is adding `"steps": [s.to_dict() for s in subtask.steps]` to the JSON payload at line 1306.

### TS parseTaskFromProto is lossy

**File**: `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts:787,806`

`parseTaskFromProto` only maps `agent` and `prompt` from proto steps — `agent_config_json` and `abort_on_failure` are dropped on the return path (read from proto). This means any new fields added to `WorkflowStepProto` would also be lost in the TS parse path unless explicitly added here.

### AgentOutput not in proto / not serializable across NATS

`AgentOutput` is a Python-only dataclass (`sandbox.py:140`). It is NOT defined in proto or Rust. For structured artifact passing across workers (remote execution), `AgentOutput` would need to be serialized into the NATS event data map (which is `map<string,string>`) or a new proto message. Currently, only `summary` and `file_changes` are passed back in the `subtask_completed` event (nats_worker.py:1546-1557).

### Parallel step worktree conflict — not fully researched

The worktree conflict for parallel steps is identified but the mitigation options (per-step worktree, read-only parallel, lock-based) would need separate research/design. The `FileIntentTracker` (scheduler.ts:256) and `ConflictDetector` (worker.py imports) could potentially be extended, but this is design work, not research.
