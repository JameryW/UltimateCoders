# Remove Dead enforce_night_window JobMetadata Field

## Background

The scheduler `JobMetadata` struct has a `enforce_night_window: bool` field
marked `#[allow(dead_code)] // Used in PR4 for per-job night window control`.
PR4 never landed (no scheduler PR4 task exists; the only `PR4` git log hit
is an unrelated TUI commit). The field is written in 3 constructor sites
(always `true`) but never read — `check_night_window()` (service.rs:292)
enforces the **global** `night_window: Arc<RwLock<Option<NightWindow>>>`,
not per-job. The per-job control layer was never built.

This is the last `#[allow(dead_code)]` field in the scheduler that is
genuinely dead (not proto-generated, not a live future knob with a real
plan). Prior session work (PRs #237/#238/#239) cleared DockerSandbox,
SandboxConfig.backend, and the circuit breaker contract.

## What I already know (verified this session)

- `crates/uc-engine/src/scheduler/service.rs:63` — field def with
  `#[allow(dead_code)]` + stale PR4 comment.
- `service.rs:189, 232, 432` — 3 constructor sites, all `enforce_night_window: true`.
- `service.rs:292` — `check_night_window()` reads `self.night_window`
  (global), NOT `metadata.enforce_night_window`. No `if metadata.enforce_night_window`
  branch exists anywhere (grep `enforce_night_window` → only the def + 3 writes).
- The global `night_window` + `NightWindow` struct + `set_night_window`/
  `clear_night_window`/`check_night_window` API is LIVE — do NOT touch.
- `JobMetadata` struct itself is live (used for `task` field); only the
  `enforce_night_window` field is dead.

## The gap

A vestigial per-job bool that suggests per-job night-window control exists
when it doesn't. Dead field → delete.

## Decisions (locked)

- **D1**: Remove `enforce_night_window: bool` field + its
  `#[allow(dead_code)]` line + the stale PR4 doc comment from `JobMetadata`.
- **D2**: Remove `enforce_night_window: true,` from the 3 constructor sites
  (lines 189, 232, 432).
- **Out of scope**: the global `night_window` API; `JobMetadata.task` field;
  building a real per-job night-window control (YAGNI — add when needed).

## Acceptance Criteria

- [ ] `JobMetadata` has no `enforce_night_window` field (grep
      `enforce_night_window` in `crates/uc-engine/` → 0 hits).
- [ ] `cargo check -p uc-engine` green; `cargo test -p uc-engine` green.
- [ ] No `#[allow(dead_code)]` reintroduced for this field.

## Technical Approach

1. `service.rs`: delete the field + doc comment + `#[allow(dead_code)]` line
   (lines 61-63), keeping `JobMetadata { task, }` struct shape.
2. Delete `enforce_night_window: true,` from 3 constructor sites.
3. Verify: `cargo check -p uc-engine`, `cargo test -p uc-engine`.

## Risk

- **Low**: field never read, so deletion cannot change behavior. The only
  risk is a missed write site; grep + `cargo check` catch it. Struct is
  private (not pub), no external API breakage.
