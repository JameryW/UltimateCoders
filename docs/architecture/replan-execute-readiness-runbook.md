# Replan execute readiness runbook (V8 T18d / D14)

Status: **ready-for-human evidence only**. True replan **execute** remains
**deferred** on [trust-eval ticket 18](agent-trust-eval/tickets/18-true-replan-deferred.md).
This document does **not** enable execute, does **not** invent a
`UC_REPLAN_EXECUTE_ENFORCE` (or similar) gate, does **not** bump digest,
does **not** re-decompose, does **not** call `admit_execution`, and does
**not** mutate DAG / TaskStore.

Authority: [D14](v8-upgrade/issues/D14-replan-execute-readiness.md),
[spec-t18d](v8-upgrade/spec-t18d.md). Precursors: T18a / [D9](v8-upgrade/issues/D9-replan-proposal-only.md),
T18b / [D12](v8-upgrade/issues/D12-replan-execute-checklist.md),
T18c / [D13](v8-upgrade/issues/D13-replan-accounting.md). Durable-runtime
contract lockstep lives in
[durable-runtime-upgrade-notes.md](durable-runtime-upgrade-notes.md).

Dry-run readiness (no mutation): `python scripts/check-replan-execute-readiness.py`.

## What this readiness is (and is not)

| Layer | Role after T18d |
| --- | --- |
| Proposal (`UC_REPLAN_PROPOSAL_ENFORCE`) | Structured `ProposalKind.replan` only — no new DAG |
| Approve checklist (`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE`) | Attention NeedsHuman + audit — no `admit_execution` |
| Accounting (`UC_REPLAN_ACCOUNTING_ENFORCE`) | Durable `WorkCase.replans_used` quota — no digest bump |
| Completion conditions (`UC_COMPLETION_CONDITIONS_ENFORCE`) | Product gate precursor ticket 18 still requires trusted use |
| True replan execute | **Still deferred** — not authorized by this runbook |

**Never** treat precursor flags being on as permission to rewrite the contract
digest or admit a new execution graph. Silent DAG rewrite would violate D11 / D9.

## Precursor flags (report-only)

Truthy tokens match UC enforce flags (Python `_TRUTHY_ENV` / Rust
`enforce_flag_enabled`): strip + ascii-lowercase in `{1, true, yes, on}`.
Unset / empty / any other value stays **off**. Defaults remain **off**.

| Variable | Precursor | Opens |
| --- | --- | --- |
| `UC_REPLAN_PROPOSAL_ENFORCE` | T18a | Watchdog `replan` → structured proposal |
| `UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` | T18b | Approve `replan` → Attention checklist + audit |
| `UC_REPLAN_ACCOUNTING_ENFORCE` | T18c | Gateway-owned `replans_used` accounting |
| `UC_COMPLETION_CONDITIONS_ENFORCE` | Ticket 18 blocker | Completion-conditions product enforce |

Ops table: [operations.md](../governance/operations.md) §1.2.

## Prerequisites before any future execute authorization

1. **Precursors understood.** Run the dry-run script and record which precursor
   flags are on in the target environment. Enabling precursors is **not**
   execute.
2. **Completion-conditions product use trusted.** Ticket 18 stays blocked until
   `UC_COMPLETION_CONDITIONS_ENFORCE` is stable in production use — not merely
   set in a lab env.
3. **Gateway / worker contract lockstep.** Follow
   [durable-runtime-upgrade-notes.md](durable-runtime-upgrade-notes.md) §1:
   upgrade and restart gateway and workers in one lockstep window (prefer
   **gateway first**, then workers, per `AGENTS.md` contract-version notes).
   No gray rollout for `CONTRACT_VERSION`.
4. **Explicit human/ops authorization** for true execute — a future decision /
   ticket beyond T18d. This runbook alone is not that authorization.

## Verify (dry-run only)

1. Run `python scripts/check-replan-execute-readiness.py`.
2. Clear hard blockers (today: missing runbook file).
3. Confirm the printed checklist reports precursor / completion-conditions flag
   states and that the script printed **no** claim that execute is enabled.
4. Re-run after any precursor flag change; the probe still must not mutate env
   or Compose.

## Rollback mindset (precursors)

If an environment enabled T18a–c precursors and needs to walk back:

1. Unset (or set non-truthy) the relevant `UC_REPLAN_*_ENFORCE` /
   `UC_COMPLETION_CONDITIONS_ENFORCE` vars on the affected processes.
2. Restart gateway / workers as needed so flag reads re-apply.
3. TaskStore / graph / digest remain unchanged by T18a–c paths when flags go
   off; no execute gate was ever flipped by T18d.

## Defaults that must stay deferred

- No `UC_REPLAN_EXECUTE_ENFORCE` (or similar) enablement gate is introduced.
- Tracked Compose / image defaults do **not** enable true replan execute.
- This runbook and `scripts/check-replan-execute-readiness.py` **never** set
  env vars and **never** rewrite Compose files.
- Digest bump / re-decompose / `admit_execution` remain out of scope until
  ticket 18 is explicitly unblocked.

## Related

- V8 map: [v8-upgrade/map.md](v8-upgrade/map.md)
- Agent OS gap table: [agent-os-architecture.md](../agent-os-architecture.md) §5
- Graph cutover sibling (same ops class): [graph-cutover-runbook.md](graph-cutover-runbook.md)
