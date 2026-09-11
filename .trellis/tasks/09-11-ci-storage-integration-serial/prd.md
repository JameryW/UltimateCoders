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
