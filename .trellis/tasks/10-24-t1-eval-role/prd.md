# T1: eval_role on BenchmarkResult and OracleVerdict

## Source ticket

[docs/architecture/agent-trust-eval/tickets/01-eval-role-on-results.md](../../../docs/architecture/agent-trust-eval/tickets/01-eval-role-on-results.md)  
Spec: [docs/architecture/agent-trust-eval/spec-p0.md](../../../docs/architecture/agent-trust-eval/spec-p0.md) (D1)  
Decision: [D1-three-benchmark-split.md](../../../docs/architecture/agent-trust-eval/issues/D1-three-benchmark-split.md)

## Goal

Add required `eval_role ∈ {search, select, report}` on `BenchmarkResult` and `OracleVerdict`, with identity helpers so select/search cannot satisfy report-only consumers. UC Oracle accept may still consume select. Public uplift source change is **out of scope** (ticket 02).

## Seams (confirmed by P0 spec)

- `BenchmarkResult` / `OracleVerdict` construction + `to_dict` / `from_dict`
- Report-identity helper(s) used by future uplift path (ticket 02 can call them)
- Feature flag default **off**: when off, role is still carried (default `select` for legacy dicts missing the key); identity **enforcement** (reject non-report where report required) only when flag on

## Acceptance criteria (from ticket)

- [ ] `BenchmarkResult` and `OracleVerdict` always carry `eval_role`
- [ ] Select-tagged values rejected by APIs/helpers that require report identity (flag on)
- [ ] Search-tagged values cannot be labeled or stored as report (flag on)
- [ ] Existing accept path can still consume select-role evidence
- [ ] Tests at Oracle / result seam
- [ ] Enforcement behind feature flag defaulting off

## Suggested flag

`UC_EVAL_ROLE_ENFORCE` — empty/unset/false → off; truthy → enforce identity rules.

## Out of scope

Ticket 02 report remasure / uplift wiring; D2 re-eval; D3 completion_conditions; changing MetaInfer publish path.

## Test approach

TDD at the Oracle/result seam (`tests/python/test_inference_oracle.py` and/or new focused tests). Prefer extending existing Oracle tests.
