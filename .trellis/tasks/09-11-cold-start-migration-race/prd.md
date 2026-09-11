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
* CI job serialisation → `09-11-ci-storage-integration-serial` (stopgap, lands
  independently).

## Blocked by

Nothing — independent of the CI task. The CI task is the workaround, this is the
fix; either can land first.
