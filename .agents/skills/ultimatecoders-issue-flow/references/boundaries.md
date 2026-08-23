# Workflow Boundaries

## One owner per concern

| Concern | Owner |
|---|---|
| Product discovery and domain vocabulary | `grill-with-docs`, `domain-modeling` |
| Large, uncertain planning | `wayfinder` |
| Disposable decision evidence | `prototype` |
| Feature specification, tickets, and blockers | Active tracker via `to-spec` and `to-tickets` |
| Isolated implementation context and project checks | Trellis |
| Test-first loop | `tdd` |
| Standards-versus-spec assessment | `code-review` |
| Final task state and commit approval | Trellis |

## Ticket handoff minimum

A Trellis PRD may point at a tracker ticket and carry its implementation-relevant facts. It must not recreate the ticket's blockers or independently mark the ticket complete. This keeps one graph authoritative and makes simultaneous Codex tasks safe to reason about.

## Parallelism

Only tickets that are both unblocked in the tracker and non-overlapping in their intended file scope may be worked in parallel. If the scope is uncertain, keep the work sequential until planning settles it.
