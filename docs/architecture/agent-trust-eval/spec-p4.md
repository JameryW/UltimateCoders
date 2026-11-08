# Spec P4 — Workload role and the report hold-out boundary

Blocked by: D11 (decided). Builds on P0 identity (`eval_role`, report-only uplift).

## Goal

Make the search/select/report split an identity property of the **workload**, so a
workload used for tuning can no longer be published as the report hold-out, and make
the report remeasure run a report workload (fail closed when enforcement is on and none
is configured).

## What to build

1. **Flag** `UC_EVAL_ROLE_SPLIT_ENFORCE` (default off / unset = off).
2. **`BenchmarkSpec.eval_role: EvalRole = SELECT`** — validated via `coerce_eval_role`;
   the spec is authoritative over anything the workload emits.
3. **Runner stamping** — `BenchmarkRunner.measure` returns results whose `eval_role`
   is the spec's, on both the success path and the early correctness/compile failure
   return.
4. **`mint_report_result`** — under the flag, a non-report source is refused with a
   clear error; a report source is returned unchanged.
5. **`OptimizationWorkflow.run(..., report_benchmark=None)`** — the report remeasure
   uses `report_benchmark` when given; under the flag, missing it fails closed with an
   actionable message.
6. **Docs**: map (D11 + P4), agent-os §5 note, and the D1 follow-on marked resolved at
   the identity layer.

## Role provenance (T27)

A resumed experiment must keep the role it was measured under:

- a `report_benchmark` whose spec does not declare `report` is refused **before** any
  measurement (the remeasure would otherwise fail late as a mint error);
- checkpoint.json records `eval_role`; resuming with a different declared role is
  refused, naming both;
- a legacy checkpoint with no `eval_role` may only be resumed by a `select` run — a
  `report`/`search` run would inherit numbers nobody declared a role for.

## Acceptance criteria

- [ ] A spec declared `report` produces report-role results (success and early return).
- [ ] A spec declared `select` produces select-role results even if the workload emits
      a different role.
- [ ] Flag off: `mint_report_result` behaves exactly as today.
- [ ] Flag on: minting report from a select/search result raises; minting from a report
      result is a no-op.
- [ ] Flag on + no `report_benchmark`: the report remeasure fails closed.
- [ ] Flag on + `report_benchmark`: the remeasure uses it (and its role is report).
- [ ] A non-report `report_benchmark` is refused before any measurement (T27).
- [ ] A resume that switches the declared role is refused; a legacy checkpoint resumes
      only as `select` (T27).
- [ ] Existing eval-role / report-uplift / workflow suites stay green.

## Out of scope

- Authoring three disjoint corpora (ops)
- A dataset/corpus format or registry
- Changing `search` feedback semantics
