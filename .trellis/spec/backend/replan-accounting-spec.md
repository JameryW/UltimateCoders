# Replan Accounting Spec (T18c)

> V8 follow-on / D13: flag-gated durable `WorkCase.replans_used` so the
> gateway owns the replan staircase counter. Accounting only — never bumps
> digest, re-decomposes, calls `admit_execution`, or mutates TaskStore/graph.
> Distinct from T18b checklist (`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE`) and from
> full T18 execute (trust-eval ticket 18).

---

## 1. Scope / Trigger

Modules: `crates/uc-types/src/governance/workcase.rs`,
`crates/uc-grpc/src/governance/proactive_service.rs` (`report_stall`),
`crates/uc-grpc/src/governance/case_service.rs` (`CaseView`),
`crates/uc-grpc/proto/engine.proto` (`CaseProto` / `ReportStallResponse`),
`python/ultimate_coders/proactive/watchdog_input.py`.

Trigger: changes to `UC_REPLAN_ACCOUNTING_ENFORCE`, `WorkCase.replans_used`,
or the stall→decide accounting path. Do **not** call `admit_execution` /
`mark_executed` / `_decompose_task`. Do **not** bump contract digest or
rewrite the live DAG.

---

## 2. Signatures

```rust
REPLAN_ACCOUNTING_ENFORCE_ENV = "UC_REPLAN_ACCOUNTING_ENFORCE"
// truthy: 1 / true / yes / on (case-insensitive); default off

WorkCase.replans_used: u32  // serde/proto default 0

async fn report_stall(...) -> OpResult<StallVerdictView>
// StallVerdictView carries replans_used + replans_remaining
```

---

## 3. Contracts

1. Flag off → client-supplied `request.replans_used` drives the staircase
   (legacy); `WorkCase.replans_used` is not mutated.
2. Flag on + `case_id` present → load case; stored counter feeds
   `WatchdogSignal.replans_used` (ignore client shadow; warn on divergence).
3. Flag on + `replan` verdict → CAS-increment `case.replans_used` once and
   persist; response returns authoritative used / remaining.
4. Flag on + exhausted stored budget → `escalate_needs_human` (no silent
   extra replan); escalate does not increment.
5. Flag on + missing / blank `case_id` → warn and skip durable path (do not
   invent a case); fall back to client shadow.
6. Stall / case views expose updated counters (`ReportStallResponse.replans_used`,
   `CaseProto.replans_used`).

---

## 4. Validation & Error Matrix

| Condition | Behaviour |
|---|---|
| Flag unset / falsey | legacy client shadow; no case mutation |
| Flag on, case present, replan | increment once; return authoritative counters |
| Flag on, case present, budget exhausted | escalate; counter unchanged |
| Flag on, no case_id / case missing | warn; skip durable; client shadow |
| CAS contention after retries | warn; stall verdict still returned |

---

## 5. Good / Base / Bad

- **Good:** two stalls with flag on increment 0→1→2 then escalate
- **Base:** flag off → same as pre-T18c client-supplied path
- **Bad:** digest bump; inventing a case; trusting a lying client shadow when flag on

---

## 6. Tests

| Contract | Test |
|---|---|
| Flag off / on / exhaustion / missing case | `uc-grpc` `proactive_service` accounting tests |
| Serde default 0 | `uc-types` `workcase` `replans_used_*` |
| Python prefers gateway counters | `tests/python/test_proactive_watchdog.py` |

---

## 7. Wrong vs Correct

| Wrong | Correct |
|---|---|
| Auto `admit_execution` after replan | Count only; execute stays deferred |
| Invent a WorkCase when case_id absent | Warn and skip durable path |
| Keep using client `replans_used` when flag on | Stored case counter is authoritative |
