# D22 — Scope a merge receipt to the branches it carried

Status: decided
Decided: 2026-11-09

## Question

D18 sources the watchdog's `preserve_effects` from **confirmed** `tool_effects`, scoped by
subtask branch or MetaInfer `operation_id` prefix, and deliberately leaves anything
unscopable empty — naming `push-main` as the example: its effect id is
`(remote, base_branch, head)`, which carries no subtask identity. Is that gap closable
without inventing a mapping, or does it stay empty?

## Decision

**Close it with the fact the arbiter already holds: the set of branches a merge carried.**

1. The merge receipt records `request.merged_branches` — the `uc/subtask/<id>` branches
   the publish actually carried, sorted, taken from the arbiter's own merge loop.
2. `confirmed_effect_ids(branches=…)` matches a **`CONFIRMED`** `push-main` record when
   any of its `merged_branches` is one of the queried branches. Identity is untouched:
   the effect id still hashes `(remote, base, head)`.
3. A publish whose branch set is unknown (no merge list: rev-parse still failed, or the
   receipt pre-dates this decision) **stays unscopable and returns nothing** — D18's
   "prefer less protection to a pretended one" still governs the fallback.
4. Only `CONFIRMED` counts, as before: a `push-main` that is `UNKNOWN` must be
   reconciled, never preserved.

## Why

The mapping D18 said was missing is not missing — the arbiter merges `uc/subtask/<id>`
branches and knows which ones it just published. Recording that on the receipt turns the
gap into one field and one match, with no new storage, no new id scheme, and no guessing
about which task "owns" a trunk commit.

## Boundary

- No change to fix ids, no new namespace, no change to how merges are ordered.
- Does not scope arbitrary trunk commits to tasks: a merge receipt covers the attempts
  whose branches it carried, nothing broader.
- Unscopable receipts stay empty (unchanged from D18).

## Implementation ticket

T39: record `merged_branches` on the receipt; match it in `confirmed_effect_ids`; tests.
