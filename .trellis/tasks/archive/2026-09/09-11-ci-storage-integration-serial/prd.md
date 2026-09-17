# Run the storage-integration suite serially (cold-DB migration race)

Backlog. Found while landing PR #628; recorded there as "known limits".

## Goal

`.github/workflows/ci-rust.yml` job `storage-integration` runs
`cargo test --features storage -- --ignored` (parallel) against a **cold**
compose database. That is the exact combination that reproducibly fails the
PostgreSQL tests with a migration race. It passed on the #628 merge only by
timing, and because it is `if: push to main || workflow_dispatch` there is no PR
gate that would catch it.

## Evidence

* Repro recipe: `crates/uc-engine/tests/storage_integration.rs` header.
* Cold DB + parallel: **4 of 7 failed, 3 out of 3 runs**.
* Cold DB + `--test-threads=1`: **7 of 7 passed, 2 of 2 runs** (~0.66s).
* Warm DB + parallel: 7 of 7 passed — which is why it hides on developer boxes.
* Error: `duplicate key value violates unique constraint
  "pg_type_typname_nsp_index"`, surfacing as `Migration error (repos)` from
  `PostgresMetadataStore::new()`.

## Requirements

* Add `--test-threads=1` to the `storage-integration` run step, with a comment
  naming the cause (constructor-runs-migrations, not "slow CI").
* Same treatment for any other job that runs `--ignored` against a cold database.

## Acceptance criteria

* The job's command carries the flag and the comment explains *why it is
  correctness, not tuning*.
* Verified against a cold database, not just a warm dev one. The user's local
  compose stack currently has TiKV/Qdrant/PG healthy, so a local full-suite run
  is feasible; otherwise validate on a draft PR.
* No test was skipped, filtered out, or renamed to make the job green.

## Out of Scope

* The underlying concurrent-migration race itself →
  `09-11-cold-start-migration-race`. This task only stops CI from tripping over
  it; it does not make product start-up safe.

## Notes

Serialising the whole workspace suite lengthens that job; it is main-only, and
the correctness cost of a flaky gate (a red main nobody trusts) is worse than
extra minutes.

## Completion Log (2026-09-17) — closed as **superseded**, not implemented

**Outcome: the flag was never added, and it is no longer needed.** This task is closed as
obsolete rather than done. Do **not** "finish" it by adding `--test-threads=1` to the
`storage-integration` job: that would re-establish a workaround as a contract, and it is
the fix — not the dodge — that landed.

**Why the premise expired.** This PRD's own Out of Scope pointed at
`09-11-cold-start-migration-race`, and that task closed the *product* defect instead of
serialising around it: `crates/uc-engine/src/migration_lock.rs` holds a session-scoped
advisory lock (`hold_schema_migrations_lock`) around the migration body, wired into **all
three** entry points — `metadata/postgres.rs:146` ("metadata"), `scheduler/migration.rs:22`
("scheduler") and `graph_store.rs:841` ("graph", which is the path this task's failing
suite exercises). Its Completion Log measured the before/after directly:

| scenario | before | after |
|---|---|---|
| cold DB, 4 concurrent constructors | 3 of 4 failed, 3/3 runs | 0 failed, 5/5 runs |
| cold DB, whole ignored `postgres` suite in parallel | 4 of 7 failed | 8 of 8 passed |

and it instructed, in the same file: *"That task should be reduced to a speed/robustness
preference or closed, not merged as if it still prevented failures."* #629 was closed
2026-09-11 as **superseded by #631**.

**Acceptance criteria re-checked on 2026-09-17.** Criterion 1 — "the job's command carries
the flag and the comment explains why it is correctness, not tuning" — is **literally still
unmet**: `.github/workflows/ci-rust.yml` still runs
`cargo test --features storage -- --ignored` in the `storage-integration` job. The flag and
that phrase do exist, but on the **other** `--ignored` job (`postgres-integration`, lines
130/137), whose parallel run was never the failure this PRD documented.

**Empirical check that the race is really gone (cold + parallel, on main).** The
`storage-integration` job still runs the ignored suite in parallel against a **cold**
compose database — the exact combination this PRD reported as failing 3 runs out of 3 — and
it passed on the most recent Rust CI run (`322a571`, 2026-09-16, run `35069816244`): every
`test result:` line reads `ok.`, **0 failed**, and the log contains **0 `SKIP:` lines**
(`graph_store_integration` 21 passed, `merge_grant_integration` 4, `granular_cancel_e2e` 2,
`pause_grace_diamond` 1). So this is not a workaround that is merely unapplied — the
condition it worked around **no longer exists**.

**Not carried forward.** The *speed* variant of the idea (serialise the suite to make it
faster/more robust) is dropped rather than deferred: the job is main-only, `--test-threads=1`
would lengthen it, and the separate `postgres-integration` job already pays that cost where
its own race was observed.

**Tracker**: #629 (closed). **Superseded by**: `09-11-cold-start-migration-race`
(archived 2026-09-11, PR #631). **Adjacent item handed to this review**: the stale
`--test-threads=1` justification inside the `postgres-integration` step — corrected in
`ci-rust.yml` on the same day (comment-only, behaviour unchanged).
