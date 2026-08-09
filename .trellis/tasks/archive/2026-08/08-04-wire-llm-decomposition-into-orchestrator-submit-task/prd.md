# Wire LLM decomposition into Orchestrator.submit_task

## Goal

Replace the newline-split decomposition in `Orchestrator.submit_task` with real LLM decomposition. The orchestrator stores `llm_client` (line 95) but never uses it — `submit_task` (line 168) does `description.split("\n")`, so a natural-language task description becomes a single subtask. This is the missing core capability: the scheduler (activated in #544/#545/#553) fires tasks with natural-language descriptions, but the Python orchestrator can't decompose them into real subtasks.

Two decomposition engines exist but are unwired:
1. **Sandbox `execute_decompose`** (`sandbox.py:377`) + `DecomposeAdapter` (`sandbox.py:604`) — runs the `claude` CLI (`--max-turns 1 --output-format json`), parsed by `parse_decomposition_output` (`sandbox.py:689`). Read-only, no sandbox handle needed.
2. **`llm_client`** (stored on Orchestrator, line 95) — a generic LLM client; never called for decomposition.

## What I already know

* `Orchestrator.submit_task` (`orchestrator.py:149`) — `lines = description.split("\n")` (line 168). Comment (line 158): "The full Orchestrator used LLM decomposition; this minimal version splits by newlines."
* `SandboxManager.execute_decompose` (`sandbox.py:377`) — wraps `_execute_subprocess`, read-only. The `DecomposeAdapter.build_request` builds the claude CLI invocation.
* `parse_decomposition_output` (`sandbox.py:689`) — parses the JSON subtask list from claude's output.
* `Orchestrator.__init__` (line 89) takes `llm_client` + `sandbox_manager`? — check. The orchestrator may not have a sandbox_manager reference (it's a minimal class).
* `nats_worker.py:1087` constructs `Orchestrator(engine=, nats_publisher=, llm_client=, codegraph_client=, merge_arbiter=)` — passes `llm_client` but it's stored unused.
* Scheduler's `NatsSubmitDispatcher` (#553) publishes task descriptions (from `uc.scheduler.yaml`) that are natural-language → need LLM decomposition, not newline-split.

## Decision (ADR-lite)

**Context**: Orchestrator.submit_task does newline-split decomposition (line 168); llm_client stored (line 95) but never used. Scheduler fires natural-language descriptions that need real decomposition.
**Decision**: **Engine B — llm_client direct.** Build a decomposition prompt, call `llm_client.complete(prompt)`, reuse the existing `parse_decomposition_output` (sandbox.py:689 — handles markdown fences + JSON wrapping, returns subtask dicts with description/depends_on/file_constraints/expected_output). Map to `Subtask` objects. No sandbox_manager injection needed (llm_client already stored).
**Fallback**: if llm_client is None OR complete() raises OR parse fails → fall back to newline-split (graceful, non-fatal — task still runs).
**Consequences**: Reuses the existing parser (no parser duplication). Prompt schema must match what parse_decomposition_output expects (JSON array of {description, depends_on, file_constraints, expected_output}).

## Open Questions

* **Which engine?** (A) sandbox execute_decompose (claude CLI, ready) or (B) llm_client direct (build prompt+parse). Recommend (A) — it's complete (DecomposeAdapter + parse_decomposition_output exist), just unwired. The orchestrator needs a sandbox_manager injection (nats_worker passes it? check).
* **Fallback on LLM failure**: if decomposition fails (claude CLI errors, bad JSON, timeout), fall back to newline-split? Or fail the task? Recommend: fall back to newline-split (graceful — task still runs, single subtask if no newlines).
* **Decomposition prompt**: the DecomposeAdapter receives a `prompt` param — what prompt? Check if a decomposition prompt template exists, or build one ("Decompose this task into subtasks as JSON: {description}").
* **Subtask structure**: `parse_decomposition_output` returns `list[dict]` — verify the shape matches what `Orchestrator.submit_task` builds (Subtask with id, description, depends_on, files, etc.).

## Requirements (evolving)

* `Orchestrator.submit_task` uses LLM decomposition (sandbox `execute_decompose` + `DecomposeAdapter`) instead of newline-split when an LLM/sandbox is available.
* Fallback to newline-split on LLM failure (graceful, non-fatal).
* Decomposition prompt template (if none exists).
* Subtask shape from `parse_decomposition_output` mapped to the orchestrator's `Subtask` objects.
* Tests: LLM decomposition path (mock the sandbox), fallback on failure, subtask-shape mapping.

## Acceptance Criteria

* [ ] `submit_task` calls LLM decomposition when sandbox/llm available
* [ ] Fallback to newline-split on failure (graceful)
* [ ] Decomposition prompt + parse wired
* [ ] Subtask shape mapped correctly
* [ ] Tests: decompose path, fallback, mapping
* [ ] CI green

## Definition of Done

* Tests added
* Lint/CI green
* CLAUDE.md note (the "minimal version" comment in orchestrator.py:158 becomes accurate — full decomposition now wired)

## Out of Scope

* Changing the scheduler (#553 publishes fine)
* LLM synthesis in ResultAggregator (separate)
* Multi-agent step workflows (existing `steps` field)

## Technical Notes

* `orchestrator.py:149` — `submit_task` (newline-split, line 168)
* `orchestrator.py:89` — `__init__` (llm_client stored, line 95)
* `sandbox.py:377` — `execute_decompose`
* `sandbox.py:604` — `DecomposeAdapter` (claude CLI)
* `sandbox.py:689` — `parse_decomposition_output`
* `nats_worker.py:1087` — Orchestrator construction (llm_client passed)
* `nats_worker.py:1318` — the submit_task call site (scheduler path)
* [[scheduler-activation-feature-2026-08-03]] — scheduler fires natural-language descriptions
