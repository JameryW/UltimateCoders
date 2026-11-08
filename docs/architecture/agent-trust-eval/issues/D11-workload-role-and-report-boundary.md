# D11 — Workload role and the report hold-out boundary

Status: decided
Decided: 2026-11-09

## Question

D1 decided the P0 two-way split (select vs report hold-out) and deferred the strict
three-way search/select/report **dataset** split to P1. Today the role labels the
*result*, not the *workload*: the report remeasure measures with the same
`BenchmarkRunner` the iteration loop used, then mints `eval_role=report`. So a workload
used for tuning can be published as the report hold-out — the exact leakage the split
exists to prevent. What decides the split now?

## Decision

**Move the role one level up: the workload declares it, the runner stamps it, and a
report remeasure must run a report workload. Enforcement is a flag; corpus authoring
stays an ops concern.**

1. **`BenchmarkSpec.eval_role`** (default `select`) declares what the workload may be
   used for. The spec is authoritative: a workload's own emitted `eval_role` is
   overridden, because the workload is the thing under measurement, not the policy
   that governs it.
2. **The runner stamps that role on every result it produces** — including the early
   return when the workload fails correctness/compile. No more "always SELECT, then
   relabel".
3. **`mint_report_result` refuses a cross-role mint** under
   `UC_EVAL_ROLE_SPLIT_ENFORCE`: minting `report` from a select/search result is the
   leakage itself. An already-report result is returned unchanged (idempotent).
4. **A report remeasure runs a report workload**: `OptimizationWorkflow.run(...,
   report_benchmark=...)`. Under the flag a missing report runner **fails closed**;
   flag off keeps today's single-runner behaviour, and the remeasure is then recorded
   as using the tuning workload rather than pretending otherwise.
5. **Strict disjoint data files remain authoring/ops** (who authors the corpora, where
   they live, how leakage into the report set is prevented). This decision fixes the
   identity and the enforcement point; it does not invent a dataset format.

## Why

The identity layer is where leakage can be made *impossible*; everything above it
(corpus authoring, labels, review) can only be made *visible*. One field on the
workload plus one stamp in the runner is a smaller change than a dataset format, and it
is the piece every later split mechanism needs anyway.

## Boundary

- No new storage, no dataset/corpus format, no second Oracle.
- `search` semantics unchanged apart from carrying the spec's role.
- Flag default off; flag off is byte-identical to today.
- Authoring three disjoint corpora is not decided here.

## Implementation ticket

T23: `BenchmarkSpec.eval_role` + runner stamping + `mint_report_result` enforcement +
`report_benchmark` on the workflow + focused tests.
