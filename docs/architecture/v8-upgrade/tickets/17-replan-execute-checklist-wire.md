# 17 — Replan execute checklist wire (T18b)

**What to build:** Flag-gated post-approve Attention + audit for `ProposalKind::Replan`. Primary seam: `decide_proposal` in `crates/uc-grpc/src/governance/proactive_service.rs`. Reuse `escalate_to_attention` / `raise_attention` patterns. Env: `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` default off.

**Blocked by:** D12

**Status:** ready

- [ ] Flag gate + approve-only Replan branch
- [ ] Attention detail includes digest/effects/proposed_request when present in evidence
- [ ] Audit action `replan_execute_checklist`
- [ ] Tests for on/off / reject / missing case_id
