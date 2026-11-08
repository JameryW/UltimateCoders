# 18: true replan deferred (placeholder)

**What to build:** Later follow-on (not P2 MVP): on stall or policy, mint a **new** decomposition / contract version and replan — distinct from same-contract agent swap (ticket 14). Blocked until product use of `completion_conditions` enforce is stable and contract version semantics are operationally trusted. This ticket documents the deferral so agents do not invent replan inside progress detection.

**Blocked by:** P0 D3 completion conditions in production use; ticket 14 should exist so MVP swap is not confused with replan.

**Status:** deferred (not grabable for P2 MVP)

**Note (2026-11-07):** V8 P3 / D9 landed **T18a** — structured `ProposalKind::Replan` (`"replan"`) proposal only, flag-gated by `UC_REPLAN_PROPOSAL_ENFORCE` (default off). See [v8-upgrade/spec-p3.md](../../v8-upgrade/spec-p3.md). Full execute / digest bump / new DAG remains deferred on this ticket.

**Note (2026-11-07, D11):** V8 MVP closed (P0–P4 / #869–#873) without satisfying this ticket; execute remains deferred outside that wave.

**Note (2026-11-07, D12 / T18b):** Approve→Attention execute **checklist** landed (`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE`, default off) under [v8-upgrade/spec-t18b.md](../../v8-upgrade/spec-t18b.md). That path projects NeedsHuman + audit only — it does **not** bump digest, re-decompose, or `admit_execution`. This ticket (full true replan execute) remains deferred.

**Note (2026-11-07, D13 / T18c):** Durable `WorkCase.replans_used` accounting landed (`UC_REPLAN_ACCOUNTING_ENFORCE`, default off) under [v8-upgrade/spec-t18c.md](../../v8-upgrade/spec-t18c.md). The gateway can enforce `max_replans` across restarts — this is quota accounting only and does **not** satisfy true replan execute (no digest bump / re-decompose / `admit_execution`).

**Note (2026-11-08, D14 / T18d):** Ops readiness runbook + dry-run probe landed under [v8-upgrade/spec-t18d.md](../../v8-upgrade/spec-t18d.md) (`docs/architecture/replan-execute-readiness-runbook.md`, `scripts/check-replan-execute-readiness.py`). That surface reports precursor / completion-conditions flag state and docs presence only — it does **not** enable execute, invent an execute env gate, bump digest, re-decompose, or `admit_execution`. This ticket (full true replan execute) remains deferred.

**Note (2026-11-08, D15):** T18 precursor ladder closed (T18a–d / #872/#875/#876/#877) under [v8-upgrade/spec-t18-closeout.md](../../v8-upgrade/spec-t18-closeout.md). Closing the precursor wave does **not** satisfy this ticket — full true replan execute (digest bump / re-decompose / `admit_execution`) remains deferred here until product-stable `UC_COMPLETION_CONDITIONS_ENFORCE` use and trusted contract-version ops.

- [ ] Explicitly out of P2 MVP scope
- [ ] Must not be implemented as a silent contract rewrite during ticket 14
- [x] T18a proposal-only precursor landed under V8 P3 (does not satisfy this ticket)
- [x] T18b approve→checklist precursor landed under D12 (does not satisfy this ticket)
- [x] T18c durable replan accounting landed under D13 (quota only; does not satisfy this ticket)
- [x] T18d ops readiness runbook + dry-run landed under D14 (readiness ≠ execute; does not satisfy this ticket)
- [x] T18 precursor wave closed under D15 (ladder closed ≠ execute; does not satisfy this ticket)
- [ ] Future ticket may split into planner ownership + digest bump / execute rules
