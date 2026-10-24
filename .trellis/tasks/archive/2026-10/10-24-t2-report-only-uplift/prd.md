# T2: report-only public uplift

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/02-report-only-public-uplift.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p0.md` (D1 accept vs publish)
- Depends on T1: `EvalRole`, `require_report_role`, `UC_EVAL_ROLE_ENFORCE`

## Goal

When **`UC_REPORT_UPLIFT_ENFORCE`** is on (default **off**):

1. After accept, run a protected **report** remasure (baseline + accepted candidate) with `eval_role=report`.
2. Public uplift / published `improvement_pct` (result field + `report.json` / manifest verdict used for publication) come **only** from that report remasure’s Oracle verdict.
3. Call `require_report_role` at the publish boundary; select/search must not populate public uplift fields.
4. Missing/failed report remasure → fail closed for publication (no select fallback).
5. Oracle **accept** loop still uses select-role evidence.

When the flag is off: preserve today’s behaviour (select-sourced nested verdicts).

## Seams

- `OptimizationWorkflow.run` — remasure after accept; attach `report_verdict` / `public_uplift` on the returned dict
- `runner._execute_inference` / `ArtifactStore.publish` — enforce report identity before writing public artifacts
- Prefer a dedicated `report_verdict` (or `public_uplift`) key so select `iterations` history stays intact

## Out of scope

D2 independent correctness re-eval; D3 completion_conditions; changing MetaInfer ranking.

## Tests

TDD: extend `test_inference_workflow.py` / `test_eval_role.py` / focused new tests for accept-vs-publish separation and no select fallback under enforce on.
