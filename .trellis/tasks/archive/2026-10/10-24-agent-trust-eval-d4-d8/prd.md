# Record Agent Trust/Eval decisions D4–D8

## Goal

User replied「继续」to grilling round 2 → adopt the proposed answers for D4–D8 and mark them decided on the wayfinder map (same pattern as D1–D3).

## Decisions to record (Proposed → Resolution)

### D4 approx-eval enablement gate (P1)
- Prune-only: approx eval may discard clearly weak candidates only.
- Enable behind flag only after Spearman/Kendall rank agreement passes on the hold-out truth set (report role per D1).
- Reported uplift must never cite approx scores.
- Bench/code change forces re-test; failure auto-disables the flag.

### D5 failure-type reviewers (P1)
- Align taxonomy with `routing.py`.
- One reviewer per failure type; thresholds via cross-validation only.
- No labeled data ⇒ that type stays off (format-gate / human only).
- Default 0.5 must not be committed as “calibrated”.

### D6 Hy-Memory wiring (P1)
- Default MCP-only; `UC_HY_MEMORY_AUTO_CONTEXT=1` enables auto inject.
- supersede-not-delete enforced on write path regardless of inject mode.
- Pin `hy-memory` version; keep off until compliance review.

### D7 stall replan ownership (P2)
- MVP: same contract, swap Agent and redispatch once; then human.
- True replan (new decomposition) deferred until D3 completion conditions land.

### D8 sandbox isolation target (P2)
- First: make `NetworkMode` None/Restricted/Full real (default toward offline + LLM egress allowlist).
- Landlock FS as a later ticket on the same map.

## Files to update

- `docs/architecture/agent-trust-eval/issues/D4`…`D8` — Status decided + Resolution
- `docs/architecture/agent-trust-eval/map.md` — Decisions so far; clear frontier
- `docs/architecture/agent-trust-eval-assessment.md` — mark D4–D8 decided

## CI hygiene

Path-mention / line-ending census pins may move when these docs change or when this task is committed. Update `test_check_spec_refs.py` / `test_check_line_endings.py` if local guards fail. Avoid new untriaged dangling path mentions (no backticks on non-tracked `*.py` paths).

## Out of scope

- Product implementation tickets / Trellis coding for approx-eval, reviewers, Hy-Memory, replan, Landlock
- GitHub Issues publication
- Graduating fog items (ontology MCP, README number CI) unless D4–D8 naturally force a ticket

## Done when

- D4–D8 files show Status decided + Resolution matching above
- Map lists them under Decisions so far; Frontier empty (or only fog)
- Assessment §四 reflects decided
- Local `check-spec-refs` + `check-line-endings` + focused pin tests pass
- Changes committed on `cursor/agent-trust-eval-wayfinder-7c92` and pushed
