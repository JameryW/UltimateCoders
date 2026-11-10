# 55 — The reconcile claim's single-owner guarantee is now tested (T55)

**Why this exists.** Every other ledger test is single-threaded, so the property that makes
reconciliation safe was **unproven**: at most one owner may hold a claim. Two owners acting
at once would mean a second push, a second `evolve-kernel` run, a duplicate stop — the
exact class the ledger exists to prevent. `begin_reconcile`'s read-modify-write runs inside
`RuntimeState.mutate` (`BEGIN IMMEDIATE`), which *should* make it atomic; "should" is not a
test.

**What the tests drive** (`tests/python/test_effect_ledger_concurrency.py`, 8 real threads
against one SQLite store, released together through a barrier):

1. **First claim:** from `UNKNOWN`, exactly one thread wins and seven get
   `EffectBusyError`; a refused claim does not consume an attempt
   (`reconcile_attempts == 1`).
2. **Expired-lease takeover:** after a crashed claim's 50ms lease lapses, exactly one
   thread takes over; `reconcile_attempts == 2` and the record says
   `reconcile lease expired; taken over`.
3. **Fencing under contention:** while the holder's lease is live, six stale threads
   calling `resolve_reconcile(owner="ops:crashed")` are **all** refused and change nothing;
   the real holder then settles exactly once (`CONFIRMED`, owner and lease cleared,
   `result` recorded).

**Result: no defect found.** The invariant holds; this task converts it from a design
claim into a regression test. The suite was run ten times in a row (30/30 passes) to show
the assertions are not timing-lucky.

**Status:** implemented (`tests/python/test_effect_ledger_concurrency.py`)

**Boundary:** threads in one process, one SQLite store. Cross-process contention (two
workers on the same store) and the Postgres path (`UC_DATABASE_URL`, advisory locks) are
not exercised here.
