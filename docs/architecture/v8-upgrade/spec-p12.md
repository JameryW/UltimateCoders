# Spec P12 — Merge receipts carry their branch set

Blocked by: D22 (decided). Small change on top of D18's effect sourcing.

## What to build

1. `MergeArbiter._push_base_branch` prepares the `push-main` receipt with
   `request.merged_branches` = the sorted branches the publish carried (from the
   merge loop's `result["merged_branches"]`).
2. `EffectLedger.confirmed_effect_ids(branches=…)` matches a `CONFIRMED` `push-main`
   whose `request.merged_branches` intersects the queried branches.
3. Tests: the receipt names its branches; the branch query matches it; an unrelated
   branch does not; unscopable/`UNKNOWN` receipts stay empty.

## Acceptance criteria

- [ ] A merge receipt's `request.merged_branches` is the sorted merge list.
- [ ] `confirmed_effect_ids(branches=[...])` returns a merge receipt for a carried
      branch and nothing for a branch it did not carry.
- [ ] Effect ids are unchanged (still `(remote, base, head)`).
- [ ] Receipts without a branch set, and non-`CONFIRMED` receipts, stay out.

## Out of scope

- Scoping trunk commits to tasks beyond "a branch this merge carried"
- Any change to fix identity or storage
