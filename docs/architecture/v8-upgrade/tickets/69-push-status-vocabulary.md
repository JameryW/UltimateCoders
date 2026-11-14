# 69 — One vocabulary for `push_status`, guarded against drift (T69)

**The structural cause of T68.** `push_status` was a bare string invented independently at each
site: `agent/workspace.py` writes `"pushed"`, `"already_pushed"`, `"failed"`, `"unknown"`,
`"busy"`; `agent/merge_arbiter.py` writes those plus `"skipped"`. Consumers matched on literals
they happened to know. T68 was the predictable result — T56/T57 added two verdicts, the worker's
delivery gate was never taught them, and a publish nobody confirmed was recorded as a completed
subtask.

Fixing that one gate does not fix the class. The next status value would drift the same way,
silently, and the test that catches it can only exist if the vocabulary exists.

**Fix.** `python/ultimate_coders/agent/push_status.py` owns the words once:

- `PushStatus` — the `Literal` type a producer may write;
- `PUSH_STATUSES` — the complete tuple;
- `APPLIED_PUSH_STATUSES` = `pushed`, `already_pushed`;
- `UNCONFIRMED_PUSH_STATUSES` = `failed`, `unknown`, `busy`;
- `TRANSIENT_PUSH_STATUSES` = `unknown`, `busy` (a retry can still settle these; a definite
  `failed` cannot).

The worker's `_delivery_verdict` now classifies through those sets rather than its own literals:

```python
if push_status in UNCONFIRMED_PUSH_STATUSES:
    return True, push_status in TRANSIENT_PUSH_STATUSES
```

Behaviour is unchanged from T68 — the sets encode exactly the verdicts T68 landed.

**Tests** (`tests/python/test_push_status_vocabulary.py`), each pair with a negative control:

- the scan itself is proven non-vacuous (it must find at least five literals, including `unknown`
  and `skipped`);
- every `push_status"] = "…"` literal written by either producer is in `PUSH_STATUSES` — this is
  the T68 detector: adding a producer status without declaring it fails;
- the delivery decision table's keys equal `PUSH_STATUSES` exactly, so a new vocabulary member
  cannot exist without a classification;
- every status is applied, unconfirmed, or deliberately skipped, and applied/unconfirmed are
  disjoint.

Measured with a throwaway mutation script (back up, mutate, run the guard, restore): regressing the
gate to know only `"failed"` fails 2 cases (`unknown`, `busy`); adding a producer literal
`"partial"` fails 1; restored → 10 passed.

**Status:** implemented (`python/ultimate_coders/agent/push_status.py`,
`python/ultimate_coders/agent/worker.py`,
`tests/python/test_push_status_vocabulary.py`)

**Boundary:** `"skipped"` means "no publish was attempted" (no remote configured) and stays a
success — it is a deliberate state, not an unconfirmed delivery. This ticket does not change any
producer's behaviour; it makes the vocabulary and its consumers checkable together.
