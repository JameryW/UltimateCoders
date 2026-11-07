# 17 — Replan execute checklist wire (T18b)

**What to build:** Flag-gated post-approve Attention + audit for `ProposalKind::Replan`. Primary seam: `decide_proposal` in `crates/uc-grpc/src/governance/proactive_service.rs`. Reuse `escalate_to_attention` / `raise_attention` patterns. Env: `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` default off.

**Blocked by:** D12

**Status:** implemented (`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` default off; `project_replan_execute_checklist` + audit action `replan_execute_checklist` in `proactive_service.rs`; `ProactiveGate::record_audit`)

- [x] Flag gate + approve-only Replan branch
- [x] Attention detail includes digest/effects/proposed_request when present in evidence
- [x] Audit action `replan_execute_checklist`
- [x] Tests for on/off / reject / missing case_id
