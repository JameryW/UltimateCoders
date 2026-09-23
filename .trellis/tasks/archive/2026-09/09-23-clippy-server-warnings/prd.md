# Clear uc-grpc clippy baseline warnings

## Goal

`cargo clippy -p uc-grpc --tests` warning-free under default features,
without breaking `--all-features`, and without behavior change.

## Background

Four pre-existing warnings in `crates/uc-grpc/src/server.rs`, all rooted in
the `PauseGraceNats` unit-under-`not(messaging)` design (`server.rs:3691`).
They predate the P2 and live-roster work; that work only surfaced them via
`--tests` runs. Full analysis in `research/notes.md`.

## Requirements

1. `:3717` (`let_unit_value`, cfg-gated `not(messaging)`): landed as
   `let _ = &nats_client;` — clippy's literal bare-statement suggestion
   was verified to trade `let_unit_value` for `path_statements` (still 1
   warning); `drop()` trades for `dropping_copy_types`. Binding a `&()`
   dodges the lint while still consuming the parameter (no
   `unused_variables`), zero behavior change.
2. `:9073`, `:9120`, `:9155` (`unit_arg` in pause-grace tests): add
   `#[allow(clippy::unit_arg)]` with a comment explaining why the hoist
   suggestion is wrong here (breaks messaging builds). Do NOT hoist.
3. No production behavior change; no signature changes; no new `allow`s
   beyond these three sites.

## Acceptance

- `cargo clippy -p uc-grpc --tests` → zero warnings (default features).
- `cargo clippy -p uc-grpc --tests --all-features` → zero new warnings,
  compiles (proves the `allow` approach is cfg-robust).
- `cargo test -p uc-grpc --lib` still green; `cargo fmt --check` clean.
- Diff is 4 small hunks in one file.

## Out of scope

- Any other clippy warnings elsewhere; refactoring `PauseGraceNats`;
  touching the pause-grace timer logic or tests' assertions.

## Decisions

- Fix shape chosen from repo evidence (cfg analysis), no user-owned
  product decision exists in this task.

## Open questions

None.
