# 18: true replan deferred (placeholder)

**What to build:** Later follow-on (not P2 MVP): on stall or policy, mint a **new** decomposition / contract version and replan — distinct from same-contract agent swap (ticket 14). Blocked until product use of `completion_conditions` enforce is stable and contract version semantics are operationally trusted. This ticket documents the deferral so agents do not invent replan inside progress detection.

**Blocked by:** P0 D3 completion conditions in production use; ticket 14 should exist so MVP swap is not confused with replan.

**Status:** deferred (not grabable for P2 MVP)

**Note (2026-11-07):** V8 P3 / D9 landed **T18a** — structured `ProposalKind::Replan` (`"replan"`) proposal only, flag-gated by `UC_REPLAN_PROPOSAL_ENFORCE` (default off). See [v8-upgrade/spec-p3.md](../../v8-upgrade/spec-p3.md). Full execute / digest bump / new DAG remains deferred on this ticket.

**Note (2026-11-07, D11):** V8 MVP closed (P0–P4 / #869–#873) without satisfying this ticket; execute remains deferred outside that wave.

- [ ] Explicitly out of P2 MVP scope
- [ ] Must not be implemented as a silent contract rewrite during ticket 14
- [x] T18a proposal-only precursor landed under V8 P3 (does not satisfy this ticket)
- [ ] Future ticket may split into planner ownership + quota + digest bump rules
