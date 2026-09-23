# Evidence

- Baseline: `cargo clippy -p uc-grpc --tests` (default features) emits
  exactly 4 warnings, all in `crates/uc-grpc/src/server.rs`. Nothing else
  in the workspace is implicated (this task changes nothing else).
- `PauseGraceNats` is `Option<async_nats::Client>` under `messaging`,
  `()` otherwise (`server.rs:3691-3694`). The type alias exists precisely
  so the `spawn_pause_grace_timer` signature stays uniform.
- Warning 1 (`:3717`, `let_unit_value`): inside
  `#[cfg(not(feature = "messaging"))]`, so the value is always `()` there.
  Clippy's own suggestion (bare `nats_client;`) is safe.
- Warnings 2-4 (`:9073`, `:9120`, `:9155`, `unit_arg`): test call sites
  passing `timer_nats()` (unit under default features). Clippy's
  hoist-and-pass-`()` suggestion would **break `--all-features` builds**,
  where the parameter is `Option<Client>`. Therefore `#[allow]` on the
  three test fns (with a comment) is the cfg-robust fix, not the hoist.
- `timer_nats()` helper (`:8734`) needs no change: it returns
  `PauseGraceNats` and is correct under both cfgs.
- No behavior change in any cfg: `:3717` drops a unit value either way;
  the test `allow`s are lint-only annotations.
