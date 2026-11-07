# Replan Execute Checklist Spec (T18b)

> V8 follow-on / D12: flag-gated Attention + audit when an operator
> **approves** a `ProposalKind.replan` proposal. Checklist only — never
> executes a new DAG, bumps digest, calls `admit_execution`, or mutates
> TaskStore. Distinct from T14 (`UC_STALL_REDISPATCH_ENFORCE`) and from
> full T18 execute (trust-eval ticket 18).

---

## 1. Scope / Trigger

Modules: `crates/uc-grpc/src/governance/proactive_service.rs`
(`decide_proposal` + `project_replan_execute_checklist`),
`crates/uc-engine/src/governance/proactive.rs` (`ProactiveGate::record_audit`),
Attention board via `governance.attention.raise`.

Trigger: changes to `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE`, the post-approve
Replan branch in `decide_proposal`, or the checklist audit action token
`replan_execute_checklist`. Do **not** call `admit_execution` /
`mark_executed` / `_decompose_task`. Do **not** bump contract digest or
rewrite the live DAG.

---

## 2. Signatures

```rust
REPLAN_EXECUTE_CHECKLIST_ENFORCE_ENV = "UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE"
// truthy: 1 / true / yes / on (case-insensitive); default off

async fn decide_proposal(...) -> OpResult<ProposalView>
async fn project_replan_execute_checklist(
    governance: &Governance,
    proposal: &ProactiveProposal,
    now: DateTime<Utc>,
)
```

Audit action: `"replan_execute_checklist"`.

---

## 3. Contracts

1. Flag off + approve Replan → no new Attention / no checklist audit.
2. Flag on + approve Replan + `case_id` present → ≤1 open Attention
   (`NeedsHuman`) with structured detail (digest / preserve_effects /
   proposed_request / human_steps); proactive audit row allowed=true.
3. Flag on + reject Replan / approve non-Replan → no checklist side-effect.
4. Missing / blank `case_id` → warn and skip (same precedent as
   `escalate_to_attention`).
5. Proposal status stays `Approved`; never `Executed` from this path.
6. Evidence prefixes `digest:` / `effect:` (T18a) populate checklist detail
   when present; missing fields render as `(none in evidence)`.

---

## 4. Validation & Error Matrix

| Condition | Behaviour |
|---|---|
| Flag unset / falsey | unchanged decide; no checklist |
| Flag on, approve Replan, case present | Attention + `replan_execute_checklist` audit |
| Flag on, reject / other kind | no checklist |
| Flag on, approve Replan, no case_id | warn; skip Attention + checklist audit |
| Attention raise fails | warn; decide still succeeds |

---

## 5. Good / Base / Bad

- **Good:** operator approves replan → worklist shows checklist with digest/effects
- **Base:** flag off → approve only flips status (pre-T18b)
- **Bad:** auto `admit_execution`; digest bump; silently rewriting DAG

---

## 6. Tests

| Contract | Test |
|---|---|
| Flag truthy parser | `replan_execute_checklist_flag_truthy_variants` |
| Flag off | `replan_approve_flag_off_projects_no_checklist` |
| Flag on + approve | `replan_approve_flag_on_raises_attention_and_audit` |
| Reject / non-replan | `replan_reject_flag_on_projects_no_checklist`, `non_replan_approve_flag_on_projects_no_checklist` |
| Missing case_id | `replan_approve_flag_on_missing_case_id_skips_projection` |

Module: `crates/uc-grpc/src/governance/proactive_service.rs` `tests`.

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Treat approve as execute | Checklist Attention only; human still acts outside the gate |
| Conflate with T14 stall redispatch | Different flag; same-contract agent swap stays T14 |
| Claim trust-eval ticket 18 done | T18b does **not** satisfy full T18 execute |
