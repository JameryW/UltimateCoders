# Enhance Multi-Agent Workflow Orchestration

## Background

PR #236 implemented the 3-step chain (claude-code → codex CR → claude-code
revise) with capability-based routing. This task extends the orchestration
with 4 capabilities: structured artifact passing, step-level retry,
conditional branch/skip, and read-only parallel step execution.

Research (`research/orchestration-extension-points.md`) mapped the full
data flow and found 2 pre-existing bugs that must be fixed first.

## What I already know (verified via research)

- `WorkflowStep` schema (proto/Rust/TS/Python): `agent`, `prompt`,
  `agent_config_json`, `abort_on_failure`. No retry/condition/parallel fields.
- `AgentOutput` (sandbox.py:140): `summary`, `file_changes`, `token_usage`,
  `success`, `stderr_tail`, `tool_calls` — structured, but only `summary` +
  `file_changes` thread forward via `_render_step_prompt` string templates.
- `_execute_steps` (worker.py:1051): sequential `for` loop, renders
  `{{prev_summary}}`/`{{prev_files}}`/`{{stepN.*}}`, aborts on failure.
- **Pre-existing bug 1**: Python `_dispatch_remote` (nats_worker.py:1295)
  omits `steps` from NATS JSON — Python-dispatched remote subtasks lose
  their workflow chain. Rust dispatch (server.rs:1747) includes steps.
- **Pre-existing bug 2**: TS `parseTaskFromProto` (grpc-bridge.ts:787) only
  maps `agent`+`prompt` — drops `agent_config_json`+`abort_on_failure`
  (and any new fields) on the proto→TS return path.
- **Parallel blocker**: `WorkspaceManager.acquire(subtask_id)` creates ONE
  worktree per subtask; parallel steps writing the same worktree conflict.
  Decision: read-only parallel only (steps with `disallowed_tools` covering
  Edit/Write).

## Decisions (locked, per user)

- **Phase 0**: Fix 2 pre-existing bugs (Python dispatch + TS parse).
- **Phase 1 — Artifact passing**: `_render_step_prompt` injects a JSON
  blob of prior `AgentOutput`s (all fields) into the prompt context as
  `{{prev_outputs_json}}` + keeps existing `{{prev_summary}}`/`{{prev_files}}`.
  Future-proof — agent parses structured data. No schema change.
- **Phase 2 — Step retry**: Add `retry_count: u32` + `retry_delay_ms: u64`
  to `WorkflowStep` (proto/Rust/TS/Python, all optional w/ serde defaults).
  Worker wraps `sandbox.execute()` in a retry loop (up to `retry_count`,
  `retry_delay_ms` backoff between attempts). Emit `step_status="retrying"`
  event with `retry_attempt` in data map. `retry_count=0` = no retry (current).
- **Phase 3 — Condition (expression language)**: Add `condition: Option<String>`
  to `WorkflowStep`. Worker evaluates a small expression against prior step
  outputs BEFORE running the step; skip if false. Language supports:
  `prev.success`, `prev.files.contains("path")`, `prev.summary.contains("x")`,
  `&&`/`||`/`!`/`==`. Empty condition = always run (current). Implement a
  tiny recursive-descent parser (no external dep — ponytail).
- **Phase 4 — Read-only parallel**: Add `parallel_group: Option<String>` to
  `WorkflowStep`. Steps sharing a non-empty `parallel_group` run via
  `asyncio.gather`. **Constraint**: a step in a parallel_group MUST declare
  read-only intent — `agent_config.disallowed_tools` must include `Edit`+
  `Write` (and Bash for safety). Worker validates this at dispatch; a
  write-capable step in a parallel_group is a hard error (subtask fails
  with a clear message). This sidesteps the worktree conflict — read-only
  steps don't mutate the worktree.
- **Out of scope**: per-step worktree (major refactor, not needed for
  read-only parallel); full write-capable parallel; cross-worker step
  distribution.

## Phasing (5 PRs)

Each phase is an independent PR, merged before the next starts.

### Phase 0 — Fix pre-existing bugs (prerequisite)
- `nats_worker.py:1295` `_dispatch_remote`: add `"steps": [s.to_dict() for s in subtask.steps]`.
- `grpc-bridge.ts:787` `parseTaskFromProto`: map `agentConfigJson` + `abortOnFailure` (and forward-compatible: surface unknown fields).
- Test: Python dispatch round-trips steps; TS parse preserves all fields.
- Verify: ruff + bun test + pytest.

### Phase 1 — Structured artifact passing
- `worker.py:1181` `_render_step_prompt`: add `{{prev_outputs_json}}` =
  JSON serialization of `step_outputs` (all AgentOutput fields, truncated
  for prompt safety). Keep existing vars.
- `decomposer.md`: document `{{prev_outputs_json}}`.
- Test: step 2 receives JSON of step 1's full AgentOutput.
- Verify: pytest.

### Phase 2 — Step retry
- proto `WorkflowStepProto`: `optional uint32 retry_count = 5;` + `optional uint32 retry_delay_ms = 6;`
- Rust `WorkflowStep` (agent.rs:124): `retry_count: u32` (serde default 0), `retry_delay_ms: u64` (default 0).
- `conversions.rs`: `step_to_proto`/`step_from_proto`.
- TS `WorkflowStepDef` + `grpc-bridge.ts` serialize + parse.
- Python `WorkflowStep` (types.py:158): `retry_count: int = 0`, `retry_delay_ms: int = 0` + to_dict/from_dict.
- `worker.py:1051` `_execute_steps`: retry loop around `sandbox.execute()`.
- `_emit_step_event`: `step_status="retrying"` + `retry_attempt` in data.
- `decomposer.md`: document `retry_count`/`retry_delay_ms`.
- Test: step with `retry_count=2` retries on failure.
- Verify: cargo + ruff + bun test + pytest + proto regen.

### Phase 3 — Condition (expression language)
- proto/Rust/TS/Python: `condition: Option<String>` / `condition?: string` / `condition: str = ""`.
- `worker.py`: `StepConditionEvaluator` — recursive-descent parser for
  `prev.success`, `prev.files.contains(x)`, `prev.summary.contains(x)`,
  `&&`/`||`/`!`/`==`/`!=`. Evaluate before running step; skip if false
  (emit `step_status="skipped"` event).
- `decomposer.md`: document `condition` + examples.
- Test: condition false → step skipped; condition true → runs.
- Verify: all.

### Phase 4 — Read-only parallel
- proto/Rust/TS/Python: `parallel_group: Option<String>`.
- `worker.py:1051`: group steps by `parallel_group`; run each group via
  `asyncio.gather` (non-empty group), sequential between groups.
- **Validation**: step in parallel_group MUST have `agent_config.disallowed_tools`
  containing `Edit`+`Write`+`Bash`. Else subtask fails with:
  `"parallel_group step must be read-only (disallowed_tools must include Edit, Write, Bash)"`.
- `_emit_step_event`: concurrent events fine (step_index per-event).
- `decomposer.md`: document `parallel_group` + read-only constraint.
- Test: 2 read-only steps same group run concurrently; write step in group → fails.
- Verify: all.

## Acceptance Criteria

- [ ] Phase 0: Python dispatch round-trips steps; TS parse keeps all fields.
- [ ] Phase 1: `{{prev_outputs_json}}` available; existing templates work.
- [ ] Phase 2: `retry_count` retries on failure; `retry_count=0` unchanged.
- [ ] Phase 3: `condition` false skips step; empty condition runs (backward compat).
- [ ] Phase 4: same `parallel_group` read-only steps run concurrently; write step rejected.
- [ ] All phases: existing 3-step chains (no new fields) behave identically.
- [ ] cargo check/test workspace, ruff, pytest 582+, bun test, tsc all green.

## Risk

- **Proto breaking**: new fields are `optional` w/ serde defaults — backward compat.
- **Parallel read-only enforcement**: if a step claims read-only but the agent
  edits (e.g. via a tool not in disallowed_tools), worktree could corrupt.
  Mitigation: hard require Edit+Write+Bash in disallowed_tools; document clearly.
- **Condition parser**: tiny surface, but a bug could skip required steps.
  Mitigation: comprehensive parser tests; empty/parse-error = run (safe default)?
  No — parse-error should FAIL the subtask (don't silently run/skip). Decision:
  parse-error → subtask fails with clear message.
- **5 PRs**: each independent, merged sequentially. Phase 0 unblocks the rest.
