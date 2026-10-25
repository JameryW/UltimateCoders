# T8: approx prune-only path and auto-disable

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/08-approx-prune-and-autodisable.md`
- Depends on T7 (`approx_eval_ready_to_prune`, agreement + latch)
- Decision D4 / spec-p1 Seam D (prune half)

## Goal

1. **Prune API** in `approx_eval.py` (same authority as T7):
   - `PruneDecision` / `PruneResult`: per-candidate `keep`/`discard` + approx score; never an `OracleVerdict` or uplift payload.
   - `prune_candidates(items, scores, *, keep_top_k: int | None = None, max_discard_fraction: float = 0.5) -> PruneResult`
     - When `not approx_eval_ready_to_prune()` → keep all (pass-through).
     - When armed: discard clearly-worse by score order (higher better by default; document direction param if needed). Survivors remain for full Oracle / BenchmarkRunner.
2. **Wire pre-filter** upstream of Oracle — prefer MetaInfer ranked-library fan-in (`metainfer_service/execution.py` before taking `library[0]`) and/or a small workflow helper. Do **not** put approx inside `Oracle.evaluate`.
3. **Uplift refusal:** helpers reject approx-sourced numbers (e.g. `source == "approx"` / `approx_score` masquerading as public uplift). Extend or wrap `published_uplift_verdict` / `require_report_role` path; fail closed under report-uplift or approx enforce as appropriate.
4. **Auto-disable on change:** `retest_agreement_on_change(fingerprint, truth_scores, approx_scores, *, previous_fingerprint=...)` — when fingerprint differs, re-run `evaluate_rank_agreement`; on fail call `disarm_approx_eval`. Store previous fingerprint in env or return for caller persistence (keep simple: compare args; disarm on fail).

## Out of scope

New DuoDiscover; changing Oracle accept math; GPU MetaInfer e2e; T9+ reviewers.

## Tests

`tests/python/test_approx_prune.py` (+ extend uplift tests): unarmed keep-all; armed prune; uplift refuses approx; fingerprint change + failed retest disarms.
