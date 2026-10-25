# T7: approx rank-agreement gate

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/07-approx-rank-agreement-gate.md`
- Decision D4: prune-only; Spearman/Kendall on report hold-out before arming
- Spec: `docs/architecture/agent-trust-eval/spec-p1.md` Seam D (agreement half only)

## Goal

Land the **agreement + arming latch** surface (prune wiring = ticket 08).

1. New inference module (e.g. `approx_eval.py`) exporting:
   - `APPROX_EVAL_ENFORCE_ENV = "UC_APPROX_EVAL_ENFORCE"`
   - `APPROX_EVAL_ARMED_ENV = "UC_APPROX_EVAL_ARMED"` (runtime latch; optional)
   - `approx_eval_enforce_enabled()` / `approx_eval_armed()` — default off
   - `RankAgreementOutcome` (frozen): `passed`, `metric`, `coefficient`, `threshold`, `n`, `reasons`
   - `evaluate_rank_agreement(truth_ranks, approx_ranks, *, metric="spearman", threshold=...) -> RankAgreementOutcome`
2. **Metric lock:** Spearman ρ (scipy-free pure Python preferred if small; else document dependency). Threshold constant explicit (e.g. `DEFAULT_SPEARMAN_THRESHOLD = 0.8`) and tested.
3. **Report-role truth set:** Callers must supply rankings derived from report-role hold-out; API rejects / fail-closes if paired labels omit report identity when `UC_EVAL_ROLE_ENFORCE` or a dedicated check is on — prefer: accept parallel sequences of scores/ids; document that truth must be report-role; optional `require_report_role` on accompanying result objects if provided.
4. **Arming:** `arm_approx_eval(outcome)` sets latch only when `outcome.passed` and enforce may be consulted later; `disarm_approx_eval()` clears. `approx_eval_ready_to_prune()` = enforce on AND armed (for T8). Missing/failed agreement ⇒ not armed.
5. Pure functions + env latch; no Oracle subclass; no uplift fields; no candidate prune yet.

## Out of scope

Prune wiring into workflow/MetaInfer (T8); auto-disable on bench change (T8); approx scorer implementation beyond what agreement needs as inputs.

## Tests

`tests/python/test_approx_rank_agreement.py`: flag defaults off; perfect rank correlation passes; anti-correlated / short / mismatched lengths fail closed; arm/disarm latch; enforce alone does not imply armed.
