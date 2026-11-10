# 57 — A claim held elsewhere is an outcome, not an exception (T57)

**The bug.** The ledger models "someone else holds this reconcile" as a first-class
outcome (`EffectBusyError`), and `_push_base_branch` already answers it for `prepare`
(`push_status="busy"`). But the two **later** points in the same publish path did not:

- `MergeArbiter._reconcile_push` called `begin_reconcile` (raises when a live claim
  exists) and `resolve_reconcile` (owner-fenced, raises when another owner's lease is
  live) with nothing handling the refusal — so in a multi-host deployment, two arbiters
  reconciling the same publish meant one of them got an `EffectBusyError` **escaping out
  of the merge pass** instead of a result dict.
- `WorkspaceManager._settle_branch_push` did the same during a subtask-branch release.

**Fix.** Treat the refusal as the modelled outcome it is:

- arbiter: `_reconcile_push` returns `"busy"` (in addition to `applied`/`absent`/
  `inconclusive`), and `_push_base_branch` maps it to `status="failed"`,
  `push_status="busy"`, `error="another reconciler holds this push; nothing pushed"` —
  **no push is attempted** on a refused claim. The transport-failure branch already
  reports `unknown` when the verdict is `busy`.
- workspace: `_settle_branch_push` returns `"inconclusive"`, so the release continues and
  the other owner's claim is left untouched (its reconciliation decides).

**Proof** (both fail on the unfixed files with `EffectBusyError`):

- `test_a_claim_held_by_another_reconciler_reports_busy` — another owner holds a live
  claim and the remote does *not* carry the commit (so an unfenced settle would say
  `absent` and authorise a retry); the arbiter must answer `busy`, push nothing, and leave
  the claim with `ops:other`.
- `tests/python/test_workspace_push_settle.py` — the fenced verdict is `inconclusive` with
  the claim untouched; an unreachable remote leaves the effect `UNKNOWN`; a landed publish
  is confirmed with its head.

**Status:** implemented (`python/ultimate_coders/agent/merge_arbiter.py`,
`python/ultimate_coders/agent/workspace.py`,
`tests/python/test_merge_arbiter_effects.py`,
`tests/python/test_workspace_push_settle.py`)

**Boundary:** only `EffectBusyError` is turned into an outcome. An `EffectTransitionError`
(an invariant violation, e.g. settling a record that is no longer `RECONCILING`) still
propagates — that is a bug to see, not a state to model.
