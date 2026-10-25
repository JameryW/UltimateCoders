# T3: independent correctness re-eval

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/03-independent-correctness-reeval.md`
- Spec: `docs/architecture/agent-trust-eval/spec-p0.md` (D2 / Seam B)
- Decision: D2 dual gate — this ticket delivers **re-eval capability only**; ticket 04 wires it into accept

## Goal

Add `BenchmarkRunner.independent_reeval(eval_root, …) -> IndependentReevalOutcome` behind **`UC_INDEPENDENT_REEVAL_ENFORCE`** (default off).

When callers use the API (tests always; product callers may check the flag):

1. Re-eval runs only under a sealed `eval_root` outside Agent-writable trees (reject or fail if eval_root is inside a provided agent workspace path when supplied).
2. Always `verify_harness()` on the protected fingerprints; unarmed/mismatched fail-closes (`attestation_ok=False`, `passed=False`).
3. Measure correctness from that sealed run; `agent_reported` must not flip `passed`.
4. Outcome is observable (`passed`, `correctness`, `attestation_ok`, `result`, `reasons`, `eval_root`).

When flag is off: existing protect/measure behaviour unchanged; API may still exist for T4 but workflow need not call it yet.

## Out of scope

Ticket 04 dual-gate accept wiring; D3 completion_conditions; changing Oracle.evaluate accept path.

## Tests

TDD at BenchmarkRunner seam (`test_independent_reeval.py` + reliability protect tests). Update inference-infra-spec with § for independent re-eval.
