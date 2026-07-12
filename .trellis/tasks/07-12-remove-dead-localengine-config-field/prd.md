# Remove Dead LocalEngine.config Field

## Background

`LocalEngine` stores `config: EngineConfig` as a private field marked
`#[allow(dead_code)]`. The field is written in all 3 constructor sites
but never read — `self.config` has zero references. The constructors
consume the `config` parameter by cloning/moving its sub-fields into the
stores/services at construction time (e.g. `config.storage.tikv_pd_endpoints.clone()`,
`config.embedding.clone()`), then store the now-redundant owner in the
struct where it is never accessed again.

Prior session work (PRs #237-#240) cleared DockerSandbox, SandboxConfig.backend,
the circuit breaker contract, and `enforce_night_window`. This is the next
verified-dead field found by auditing remaining `#[allow(dead_code)]` annotations.

## What I already know (verified this session)

- `crates/uc-engine/src/local.rs:61-62` — field def:
  ```rust
  #[allow(dead_code)]
  config: EngineConfig,
  ```
  Private (no `pub`), no `pub fn config()` accessor.
- `self.config` — 0 references across `local.rs` (grep `self\.config` → no hits).
- `health()` (local.rs:512) does NOT read `self.config` — it reports
  embedding_service/memory/etc. states, not config.
- `EngineConfig` has no `Drop` impl (grep `impl Drop` in `uc-types/` → 0) —
  no side-effect from dropping the stored copy.
- 3 constructor sites store the field:
  - `new()` (:72) → field assignment `config,` at :153
  - `new()` fallback variant (:160) → `config,` at :278
  - `new_fallback()` (:170) → `config: EngineConfig::default()` at :219
    (constructs a default purely to satisfy the dead field)
- All 3 constructors already consume `config`'s sub-fields into live
  stores (short_term, long_term, metadata_store, embedding_service,
  memory_store) via `.clone()` / `.as_deref()` / field reads.

## The gap

A vestigial stored config that suggests runtime config access exists when
it doesn't. `new_fallback()` even constructs `EngineConfig::default()` for
no reason. Dead field → delete.

## Decisions (locked)

- **D1**: Remove the `config: EngineConfig` field + `#[allow(dead_code)]`
  line + its doc comment (if any) from `LocalEngine` struct (lines 61-62).
- **D2**: Remove the `config,` field assignment from `new()` (:153) and
  the fallback `new()` (:278).
- **D3**: Remove `config: EngineConfig::default(),` from `new_fallback()`
  (:219) — this also removes the now-pointless `EngineConfig::default()`
  construction. If `new_fallback()` no longer references `EngineConfig`
  at all, drop any now-unused import.
- **Out of scope**: the `config` *parameter* of `new()` / `new_fallback_with_config()`
  (consumed at construction, live); `EngineConfig` type itself; other
  `LocalEngine` fields.

## Acceptance Criteria

- [ ] `LocalEngine` struct has no `config` field.
- [ ] `new_fallback()` does not construct `EngineConfig::default()`.
- [ ] `grep -n "self\.config\|\.config," crates/uc-engine/src/local.rs` →
      0 hits for the field (the `config.` sub-field reads in constructors
      on the local `config` param are fine — those stay).
- [ ] `cargo check -p uc-engine` green; `cargo test -p uc-engine` green.
- [ ] No `#[allow(dead_code)]` reintroduced.

## Technical Approach

1. `local.rs`: delete the field + `#[allow(dead_code)]` line (61-62).
2. Delete `config,` from the two `new()` struct literals (:153, :278).
3. Delete `config: EngineConfig::default(),` from `new_fallback()` (:219).
4. Check if `EngineConfig` import in `local.rs` is still used (the `config`
   param type of `new`/`new_fallback_with_config` still uses it) — keep
   the import.
5. Verify: `cargo check -p uc-engine`, `cargo test -p uc-engine`.

## Risk

- **Low**: field never read, no `Drop` side-effect, private with no
  accessor. Deletion cannot change behavior. The only risk is a missed
  field-write site; grep + `cargo check` catch it. `LocalEngine` is `pub`
  but the field is private — no external API breakage.
