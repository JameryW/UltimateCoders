# Serialize cold-start scheduler migrations with an advisory lock

Backlog. Product defect, surfaced by PR #628's CI job and only worked around
there by running tests serially.

## Goal

Two or more processes that start against a **fresh** PostgreSQL database at the
same time can fail permanently at boot. Every store constructor runs its own
migrations, and DDL like `CREATE TYPE` / `CREATE TABLE IF NOT EXISTS` is not safe
under concurrency: both sessions see "does not exist", one loses the insert into
the catalog.

Observed as:

```
duplicate key value violates unique constraint "pg_type_typname_nsp_index"
→ EngineError::ConnectionError("Migration error (repos): …")
```

## Why it matters here

* This deployment model explicitly supports **multiple gateway replicas** and
  cross-host workers; a scale-out against a new database can start several
  gateways at once.
* Failure is not benign: `PostgresMetadataStore::new()` maps it to `Ok` with an
  **in-memory fallback**, so a replica comes up looking healthy while its
  metadata silently does not persist. The scheduler store is stricter and
  returns `Err`, so behaviour differs between the two — see "Adjacent findings".
* `docker compose up` starting gateway + orchestrator + workers against a new
  `pg_data` volume is the realistic trigger; CI's cold-database jobs hit the same
  path.

## Requirements

* Migrations must be safe under concurrent execution: one session does the
  work, the others wait and then observe an up-to-date schema.
* Apply to **both** migration entry points (`metadata/postgres.rs`
  `run_migrations`, `scheduler/migration.rs` `run_migrations`), which currently
  duplicate the pattern rather than sharing it.
* No new failure mode: a session that dies must not leave the lock held
  (transaction- or session-scoped advisory locks release automatically).
* Bounded wait, then a clear error rather than an indefinite hang.

## Design decision needed (pick one, record the reason)

1. `pg_advisory_lock(key)` + `pg_advisory_unlock` around each migration set —
   smallest change, works across replicas, needs an agreed namespace constant.
2. Serialize by object: wrap each DDL statement and retry once on
   `duplicate key value … pg_type_typname_nsp_index` /
   `already exists, race` class errors — no lock to leak, but retry logic per
   statement.
3. Run migrations exactly once in the gateway binary, never in store
   constructors — most invasive; also changes embedded-library behaviour for
   `uc-python` consumers.

Default suggestion: **(1)**, because it also covers `CREATE INDEX`, is a single
place to reason about, and matches the "first boot wins, others wait" model.

## Acceptance criteria

* Two processes calling the same store constructor concurrently against a **cold**
  database both end up connected and migrated; neither reports
  `Migration error (…)`.
* A regression test exists that is not serialised by luck (e.g. spawn two
  `tokio::join!` constructions against a freshly created schema/database, gated
  `#[ignore]` with the live-DB suite, plus a note that it must run with
  `--test-threads>1` to be meaningful).
* `.trellis/spec/backend/database-guidelines.md` documents the rule so new
  stores inherit it instead of re-deriving it.
* Verified against a real PostgreSQL (the local compose stack suffices), not only
  the in-memory fallback.

## Adjacent findings (do not silently bundle)

* `PostgresMetadataStore::new()` swallowing a connection failure as
  `Ok` + in-memory fallback means health checks and CRUD tests can pass without a
  database. Deciding whether that should be fatal at boot is a **separate**
  product decision; note it here so it is not lost, and split it if the answer
  changes behaviour for existing deployments.
* CI job serialisation → `09-11-ci-storage-integration-serial` (**superseded by
  this fix — see Completion Log**).

## Blocked by

Nothing — independent of the CI task. The CI task is the workaround, this is the
fix; either can land first.

## Completion Log (2026-09-11)

**Wording correction.** The original write-up (here and in #629/#630) says the
race is in `CREATE TYPE`. There is no `CREATE TYPE` in these migrations. The real
mechanism is that **`CREATE TABLE` also inserts a composite row type**, so the
loser of a concurrent `CREATE TABLE IF NOT EXISTS` is rejected on
`pg_type_typname_nsp_index`. Same symptom, different statement — worth being
precise, because "add IF NOT EXISTS" is exactly the kind of non-fix someone would
reach for against the wrong reading.

**Chosen design: option (1)**, transaction-free session-scoped advisory lock held
by a dedicated connection (`pg_try_advisory_lock` + bounded 30s wait, one shared
key). Option (2) was rejected as per-statement retry logic duplicated across
~20 DDL sites; option (3) as an invasive change to library consumers. The
dedicated-connection choice is what makes the guard leak-proof: migration bodies
keep their `?` early returns and cancellation still releases the lock, because
closing the session releases it.

* `crates/uc-engine/src/migration_lock.rs` (new, storage-gated) + `lib.rs` wiring.
* One guard line in `metadata::PostgresMetadataStore::run_migrations` and
  `scheduler::migration::run_migrations`; both migration bodies unchanged.
* Regression test `schedule_concurrent_migrations_are_serialized` (four
  concurrent constructors against a freshly created database).
* Rule documented in `.trellis/spec/backend/database-guidelines.md` under
  "Concurrent cold-start safety (required for every new store)".

**Evidence (live PostgreSQL 16, local compose):**

| scenario | before | after |
|---|---|---|
| cold DB, 4 concurrent constructors | 3 of 4 failed, 3/3 runs | 0 failed, 5/5 runs |
| cold DB, whole ignored `postgres` suite in parallel | 4 of 7 failed | 8 of 8 passed |

Per-run cost of serialisation: ~0.8s (cold DB only).

**Consequence for #629**: measured after the fix, "cold + parallel" passes the
full suite 8/8, so the `--test-threads=1` flag on the main-only
`storage-integration` job is no longer load-bearing. That task should be reduced
to a speed/robustness preference or closed, not merged as if it still prevented
failures.

**Gates**: `cargo fmt --all -- --check` 0; `cargo clippy --workspace
--all-targets --all-features -- -D warnings` 0; `cargo test -p uc-engine
--features scheduler` 442 passed / 0 failed; `--no-default-features` 370;
`uc-grpc --all-features` 172+8; `uc-grpc-server` 35. Probe databases dropped, no
leftover schemas.
