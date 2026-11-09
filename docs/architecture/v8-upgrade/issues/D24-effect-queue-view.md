# D24 — The receipt queue gets a view

Status: decided
Decided: 2026-11-09

## Question

D23 exposed `GET /dashboard/api/effects`, but nobody types URLs at a terminal — the
queue only becomes a working tool if the operations dashboard shows it. Where does it
live, and what may the view do?

## Decision

**A `#/dashboard/effects` view that renders the queue read-only, one status at a time.**

1. **Placement.** A first-class view next to approvals (`收据` / `RECEIPTS`), not a
   section inside an existing panel: a receipt is not task-scoped, and burying it under
   inference or tasks would hide the one queue an operator must act on.
2. **One status at a time**, because that is what the endpoint answers. `UNKNOWN` is
   the default; `RECONCILING` offers the `lease expired` filter, which is the only
   status where "the holder died" is meaningful.
3. **Read-only.** No reconcile, kill or retry control: the same boundary as D23 — those
   actions belong to the operator CLI and the merge arbiter, which carry an identity and
   CAS version. The view explains *what* is stuck; acting stays elsewhere.
4. **Rows show state, age, holder, and scope** (`kind`, `status · age`, owner plus lease
   state, and the branch / operation the receipt was recorded against). Paging appends
   deduped by effect id and keeps held rows when a page fails, the pattern the decision
   and manifest trails already use.

## Why

D23 made the queue readable; D24 makes it *findable*. Both stop at the same line: the
dashboard never claims an authority it has no principal for.

## Boundary

- No new endpoint, no new query parameter, no mutation.
- No automatic refresh loop or alerting (an operator polling/alert story is separate).
- No changes to how receipts are written or scoped.

## Implementation ticket

T45: lib helpers + list/trail + the view registration + tests.
