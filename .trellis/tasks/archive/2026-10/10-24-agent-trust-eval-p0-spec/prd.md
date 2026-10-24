# Agent Trust/Eval P0 spec and tickets

## Goal

Publish the P0 feature spec (D1–D3) and six vertical-slice implementation tickets under `docs/architecture/agent-trust-eval/`, linked from the wayfinder map. No product code.

## Seams (recommended — adopted via「继续」)

1. `Oracle.evaluate` / `OracleVerdict` + `BenchmarkResult` — `eval_role`
2. `BenchmarkRunner.protect` / measure — independent correctness re-eval
3. `ultimate_coders.agent.contract` package — `completion_conditions` + non-LLM evaluate

## Tickets

T1 eval_role · T2 report-only uplift (←T1) · T3 independent re-eval · T4 dual-gate accept (←T3) · T5 completion_conditions · T6 adapters + re-eval condition (←T5,T3)

## Done when

- `spec-p0.md` + `tickets/01`–`06` exist and match D1–D3
- Map points at them; Status ready-for-agent on tickets
- Corpus pins updated if needed; PR opened on `cursor/agent-trust-eval-p0-spec-7c92`
