# T6: completion adapters and independent-reeval condition

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/06-adapters-and-reeval-condition.md`
- Depends on T5 (`completion_conditions`) and T3 (`IndependentReevalOutcome`)
- Decision D3: adapters under one authority; D2 composed via re-eval condition

## Goal

Extend `evaluate_completion_conditions` with condition types (no second registry):

1. **`verify_command`** — passes iff `evidence.get("verify_command_passed") is True` (caller runs the command elsewhere and supplies the boolean). Params may optionally name the command for diagnostics only.
2. **`step_condition`** — params `{"expr": "<step_condition grammar>"}`; evaluate using `ultimate_coders.agent.step_condition.evaluate` against `evidence["prev"]` (an `AgentOutput` or `None`). Parse errors → fail closed.
3. **`independent_reeval`** — passes iff evidence carries a passing sealed re-eval: prefer `evidence["independent_reeval"]` as `IndependentReevalOutcome` with `.passed is True`, or `evidence.get("independent_reeval_passed") is True`. Model/review self-approval alone must not satisfy this type.
4. Optional **`review_approved`** adapter: if present, still requires non-LLM evidence (e.g. must not alone pass — treat like self-report OR require pairing). Prefer: `review_approved` fails unless also `evidence.get("review_verified") is True` — or simply map review JSON approval into self-report fail-closed. Ticket: "must not alone bypass non-LLM evaluate" → make `review_approved` fail closed alone (same as self-report) OR require a verified flag.

Reuse **`UC_COMPLETION_CONDITIONS_ENFORCE`** / `completion_gate` (no new flag unless needed; ticket says feature flag defaults off — existing gate covers it).

## Out of scope

Wiring adapters into worker/orchestrator production paths beyond exporting types; changing step_condition grammar.

## Tests

Extend `test_completion_conditions.py` or add `test_completion_adapters.py`.
