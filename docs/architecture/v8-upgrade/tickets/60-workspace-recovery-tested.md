# 60 — The operator workspace-recovery path is now tested (T60)

**What was missing.** `RemoteJobs.recover_workspace` is the emergency path for a lease
whose worker died: compare-and-set on the lease, then host identity, then local process
birth, then the remote terminal state of every experiment in the worktree — and only then
does it mark the lease reconciled and release the GPU reservations it proved nobody is
using. **It had no tests at all**, and its reservation release had a real failure mode:

```python
for experiment in experiments:
    for resource_id in experiment.get("gpu_reservations", []):
        ResourceBudget(self.state, resource_id).release(experiment["key"])
```

One unreadable budget record raised out of the loop, so **every remaining reservation of
the recovered workspace stayed held** — after the lease had already been marked
`reconciled`, meaning nothing would try again.

**What this task does.**

1. Each reservation is released independently: a failure is logged
   (`Workspace recovery could not release reservation …`) and recorded, not fatal.
2. The result now reports what happened: `released_reservations` and
   `unreleased_reservations` alongside the reconciled lease record, so an operator does not
   have to diff `resource_budgets` by hand.
3. `tests/python/test_workspace_recovery.py` pins each guard, so a loosened one is
   visible: another host, a live local owner (process birth match), an unresolved remote
   job, a stale version (CAS), plus the happy path and the failure-isolation case.

**Proof the tests bite:** with `reconcile.py` reverted (`git stash`), the clean-recovery
test and the isolation test fail — the latter with the `RuntimeError` escaping the loop
(2 failed / 4 passed). With the change: 6 passed.

**Status:** implemented (`python/ultimate_coders/inference/reconcile.py`,
`tests/python/test_workspace_recovery.py`)

**Boundary:** the guards themselves are unchanged — this task adds the missing evidence and
the failure isolation, not new policy. Cross-host recovery still refuses to run anywhere but
the owning worker host, by design.
