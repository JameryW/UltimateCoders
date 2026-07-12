# Document Multi-Agent Workflow Step Contract in Specs

## Background

PRs #245-#249 enhanced the multi-agent workflow orchestration with 4 new
`WorkflowStep` fields (retry_count, retry_delay_ms, condition, parallel_group)
+ structured artifact passing (`{{prev_outputs_json}}`). The code is
cross-layer complete (proto/Rust/TS/Python + decomposer.md), but the
**spec layer has no record of the WorkflowStep contract** — only
`event-pipeline-spec.md` mentions workflows, and it doesn't document the
step schema. Without spec documentation, the contract exists only in code
+ the decomposer prompt, and will drift.

## What I already know (verified this session)

- `grep -rln "WorkflowStep|multi-agent|workflow chain|steps[]" .trellis/spec/backend/` → only `event-pipeline-spec.md` mentions workflows (no step schema).
- `agent-capability-spec.md` → 0 hits for WorkflowStep/steps[]/step fields.
- The full contract lives in:
  - `crates/uc-types/src/agent.rs` `WorkflowStep` struct (Rust domain)
  - `python/ultimate_coders/agent/types.py` `WorkflowStep` dataclass
  - `packages/uc-orchestrator/src/orchestrator/scheduler.ts` `WorkflowStepDef`
  - `packages/uc-orchestrator/src/agents/decomposer.md` (LLM prompt + schema — most complete docs)
  - `python/ultimate_coders/agent/worker.py` `_execute_steps` (execution semantics)
  - `python/ultimate_coders/agent/step_condition.py` (expression language)

## The gap

No spec records:
1. The `WorkflowStep` field schema (8 fields + defaults + backward-compat).
2. The template variables (`{{prev_summary}}`/`{{prev_files}}`/`{{prev_outputs_json}}`/`{{stepN.*}}`).
3. The execution semantics (sequential by default, abort_on_failure, retry loop, condition eval, parallel groups + read-only constraint).
4. The condition expression language grammar.
5. The event types emitted (step started/completed/failed/retrying/skipped).

## Decisions (locked)

- **D1**: Add a new section "Multi-Agent Workflow Steps" to
  `.trellis/spec/backend/agent-capability-spec.md` documenting the full
  `WorkflowStep` contract: field schema (table with field/type/default/
  backward-compat), template variables, execution semantics (sequential →
  retry → condition → parallel), condition expression language grammar,
  read-only parallel constraint, emitted step events. This is the canonical
  spec home — agent-capability-spec already covers worker execution.
- **D2**: Add a cross-reference in `.trellis/spec/backend/event-pipeline-spec.md`
  (where workflows are mentioned) pointing to the new agent-capability-spec
  section for the step schema.
- **D3**: Update `.trellis/spec/backend/dashboard-spec.md` if it documents
  step progress events — ensure `step_status` values include the new
  "retrying"/"skipped" (check current content first; if it already lists
  step_status values, extend them).
- **Out of scope**: TUI/dashboard rendering of step_status (separate UI work);
  decomposer.md (already documents the LLM-facing schema, no change); code
  changes (spec-only).

## Acceptance Criteria

- [ ] `agent-capability-spec.md` has a "Multi-Agent Workflow Steps" section
      with: field schema table (8 fields), template variables, execution
      semantics, condition grammar, parallel read-only constraint, step events.
- [ ] `event-pipeline-spec.md` cross-references the new section.
- [ ] `dashboard-spec.md` step_status values (if present) include retrying/skipped.
- [ ] Content matches the actual code (verify against
      `crates/uc-types/src/agent.rs` + `worker.py` + `step_condition.py` +
      `decomposer.md` — do NOT invent fields/semantics not in code).

## Technical Approach

1. Read `crates/uc-types/src/agent.rs` `WorkflowStep` (the authoritative
   field list + defaults) + `worker.py` `_execute_steps`/`_run_single_step`
   (execution order) + `step_condition.py` (grammar) + `decomposer.md`
   (template vars + examples).
2. Write the "Multi-Agent Workflow Steps" section in
   `agent-capability-spec.md` mirroring the code exactly. Field table:
   agent, prompt, agent_config_json, abort_on_failure, retry_count,
   retry_delay_ms, condition, parallel_group — each with type, default,
   backward-compat note.
3. Cross-ref in event-pipeline-spec.md.
4. Check dashboard-spec.md for step_status coverage; extend if needed.
5. Verify: no code change, so just `grep` the specs for consistency.

## Risk

- **Spec drift**: if the doc doesn't match code exactly, it misleads. Mitigation:
  read the code first, mirror it; cite the source file:line for each field.
- **Scope creep**: do NOT document TUI rendering or decomposer prompt internals
  (out of scope). Stick to the contract (schema + semantics + events).
