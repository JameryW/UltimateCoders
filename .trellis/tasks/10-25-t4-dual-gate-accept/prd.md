# T4: dual-gate Oracle accept

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/04-dual-gate-accept.md`
- Depends on T3: `BenchmarkRunner.independent_reeval` / `IndependentReevalOutcome`
- Spec: P0 D2 dual gate

## Goal

When **`UC_DUAL_GATE_ENFORCE`** is on (default **off**):

1. After select-path `oracle.evaluate`, also run `independent_reeval` on a **sealed** root (not under the Agent workspace `root`). Prefer copying needed candidate+harness into a temp sealed dir; pass `agent_workspace=str(root)` and `agent_reported=measured`.
2. Accept (`keep`) only if `verdict.accepted` **and** `reeval.passed`.
3. If attestation fails → reject even if re-eval would pass; if re-eval fails/skipped → reject even if Oracle accept would pass.
4. Record which gate failed in history/verdict reasons (e.g. `dual_gate: attestation`, `dual_gate: independent_reeval`) so events are diagnosable.
5. Agent harness self-report alone never yields accept under the flag.
6. When flag **off**: existing behaviour (verify_harness + oracle only); do not weaken fingerprint fail-closed.

## Seams

- `OptimizationWorkflow.run` accept decision after `oracle.evaluate`
- Optional helper `apply_dual_gate(verdict, reeval) -> OracleVerdict` in oracle.py or workflow
- Tests at workflow / dual-gate matrix seam

## Out of scope

D3 completion_conditions; changing report uplift (T2).
