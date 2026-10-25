# T5: completion_conditions in contract package

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/05-completion-conditions.md`
- Decision: D3 — dual namespace, export from `ultimate_coders.agent.contract`
- Spec: P0 Seam C

## Goal

Land the **home + evaluate surface** for structured completion conditions (adapters / re-eval condition type = ticket 06).

1. Keep `contract_digest` / `ContractRegistry` unchanged in responsibility (goal-change only).
2. Add structured `CompletionCondition` + `evaluate_completion_conditions(conditions, evidence) -> CompletionVerdict` exported from the contract module/package.
3. Evaluate is pure non-LLM code. Model self-report keys (`approved`, `done`, `llm_done`, etc.) **alone** cannot satisfy completion.
4. Flag **`UC_COMPLETION_CONDITIONS_ENFORCE`** default off: when off, evaluate may no-op / return satisfied for empty conditions without changing digest APIs; when on, empty conditions fail or require explicit always-true type — prefer: empty list → satisfied (nothing to check); non-empty → all must pass; unknown type → fail closed.
5. Include at least one machine-checkable built-in type for tests (e.g. `evidence_flag` requiring `evidence[name] is True`) so the seam is demoable without T6 adapters.

## Out of scope

verify_command / step_condition adapters; independent_reeval condition type (ticket 06); wiring into worker accept path beyond exporting the API.

## Tests

New `tests/python/test_completion_conditions.py` + ensure `test_contract_versioning.py` still passes.
