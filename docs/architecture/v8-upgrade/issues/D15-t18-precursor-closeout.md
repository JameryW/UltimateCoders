# D15 — T18 precursor wave closeout

Status: decided
Decided: 2026-11-08

## Question

After T18a–d (#872 / #875 / #876 / #877), should agents keep inventing more silent replan precursors under this map, or close the precursor ladder until a product decision re-opens true execute?

## Decision

**Close the T18 precursor wave.** T18a (proposal), T18b (checklist), T18c (accounting), and T18d (ops readiness) delivered the safe governance/ops precursors without digest/DAG/`admit_execution`. Mark follow-on status **complete (precursors)** with:

- True replan **execute** (trust-eval ticket 18 remainder) still deferred — blocked on product-stable `UC_COMPLETION_CONDITIONS_ENFORCE` use and trusted contract-version ops.
- No further silent precursor slices under this map without a **new** decision that explicitly re-opens execute (or another named precursor with a clear product gap).
- Learning OS (D3) and graph-primary **default-on** remain deferred / human-gated as before.

## Why

Frontier after T18d is empty of unblocked implement tickets. Keeping the precursor ladder “active” invites agents to invent T18e+/dashboard fluff or silent execute. Closure records what shipped (#872–#877) and where execute lives (ticket 18).
