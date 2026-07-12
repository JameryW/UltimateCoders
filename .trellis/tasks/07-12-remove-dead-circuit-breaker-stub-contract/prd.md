# Remove Dead Circuit Breaker Contract (Cross-Layer)

## Background

PR #237/#238 cleaned dead sandbox code. Further research
(`research/circuit-breaker-footprint.md`) found the **circuit breaker** is
dead at every layer — stub methods return hardcoded "unavailable", a real
`CircuitBreaker` class exists but is never instantiated (Python) / never
called (Rust methods), and the frontend has full display logic that can
never activate because the backend source always returns `available: False`
/ state `"unknown"`.

The feature was nominally "removed (sandbox-only mode)" per error strings,
but the entire contract (proto RPCs, stub methods, types, UI panel, specs,
tests) was left in place. This task deletes it fully.

## What I already know (verified this session + research)

See `research/circuit-breaker-footprint.md` for the complete file:line map.
Key confirmations re-run this session:

- **Rust `RetryPolicy`** (`circuit_breaker.rs:232`) — `pub use`d at
  `lib.rs:24` but **0 external consumers** (grep `RetryPolicy` across
  `crates/`/`python/`/`ts` → only def + tests in `circuit_breaker.rs`;
  Python has its own separate `RetryPolicy` at `rate_limiter.py:398`).
  Delete with the file; remove from re-export.
- **PyO3 binding** — `crates/uc-python/` has 0 CB references; Python never
  accesses `engine.circuit_breaker` / `engine.check_circuit_breaker`
  (grep clean). Rust CB is fully dead.
- **Rust `LlmRateLimiter`** (`rate_limiter.rs`, `local.rs:58` field) is
  **separate and LIVE** — do NOT touch. Only the CB half of
  `circuit_breaker.rs` + `local.rs` CB field/methods are dead.
- **Proto pipeline**: Rust proto types regenerate via `tonic_build` (build.rs)
  on `cargo build`; TS `engine_pb.ts` regenerates via
  `dashboard/package.json` `generate` script (`buf generate`). Edit
  `engine.proto` only; regenerate, never hand-edit generated files.

## The gap

A self-contained dead feature spanning 4 layers (Rust proto + engine,
Python nats_worker + dashboard + metrics + dead `CircuitBreaker` class,
frontend types + panel + hooks + App, 6 spec files, tests). Every path is
dead or stub. Deletion is atomic — leaving half leaves dangling references.

## Decisions (locked)

- **D1 (Rust engine)**: delete `crates/uc-engine/src/circuit_breaker.rs`
  (445 LOC). Remove `pub mod circuit_breaker;` (`lib.rs:7`) and the
  `pub use circuit_breaker::{...}` line (`lib.rs:24`) — including
  `RetryPolicy` (no external consumer). Remove the `circuit_breaker` field,
  3 constructor sites, `circuit_breaker()` accessor, `check_circuit_breaker`
  /`record_llm_success`/`record_llm_failure` methods, the `health()`
  `"circuit_breaker"` ComponentHealth push, and CB tests in `local.rs`.
  Update `health.components.len()` assertion `11 → 10` and the
  `components[9].name == "circuit_breaker"` test.
- **D2 (Rust proto + gRPC)**: delete 2 RPCs (`GetCircuitBreakerStatus`,
  `ResetCircuitBreaker`), 4 messages (`GetCircuitBreakerStatusRequest`,
  `CircuitBreakerStatusResponse`, `CircuitBreakerProto`,
  `RateLimiterProto`, `ResetCircuitBreakerRequest`/`Response`),
  `DashboardSnapshot.circuit_breaker` field, `SystemMetrics.circuit_breaker_state`
  field in `engine.proto`. Delete `dashboard_service.rs` impls +
  4 JSON converters + the `circuit_breaker_state`/`circuit_breaker` mappings
  + test snapshot `circuit_breaker: None`.
- **D3 (Python backend)**: delete `nats_worker.py` `_dash_getcircuitbreakerstatus`
  /`_dash_resetcircuitbreaker` + the `_build_snapshot` call site + snapshot
  field. Delete `dashboard/app.py` `/dashboard/api/circuit-breaker` route,
  `/circuit-breaker/reset` route, `_get_circuit_breaker_data()` method, the
  `_get_full_snapshot` call site, and the `_update_system_metrics` CB feed
  (line 1492-1520 `cb_state` extraction). Delete the dead
  `agent/rate_limiter.py:252 CircuitBreaker` class (0 construction sites).
- **D4 (Python metrics)**: delete `metrics.py` `circuit_breaker_state` field
  (`SystemMetrics`), `_circuit_breaker_state` aggregator state,
  `circuit_breaker_alert` `AlertConfig` flag, the `check_alerts`
  `circuit_breaker_open` alert block, the `uc_circuit_breaker_state`
  Prometheus gauge + sync mapping. Keep `SystemMetrics`/`MetricsSnapshot`
  (shared with live metrics).
- **D5 (Frontend types + panel)**: delete `CircuitBreakerInfo`,
  `RateLimiterInfo`, `CircuitBreakerData`, `circuit_breaker_state`,
  `circuit_breaker?` from `types/dashboard.ts`. Delete entire
  `CircuitBreakerPanel.tsx` file (179 LOC). Delete the 2 alert blocks +
  `circuitBreaker` prop from `alert-bar.tsx` (keep AlertBar itself). Delete
  CB display row + CSV line from `MetricsPanel.tsx`. Delete 3
  `circuit_breaker_reset` style lines from `EventLogPanel.tsx`.
- **D6 (Frontend hooks + App)**: delete CB state/fetchers/converters from
  `useDashboard.ts` + `useDashboardGrpc.ts`. Delete `CircuitBreakerPanel`
  import, panel block, `handleResetCB`, fetch refs, `cbSummary` line,
  AlertBar `circuitBreaker` prop from `App.tsx`.
- **D7 (Frontend generated)**: after `engine.proto` edit, run
  `npm run generate` (buf) to regenerate `engine_pb.ts`. Do not hand-edit.
- **D8 (Specs)**: remove CB sections from `dashboard-spec.md`,
  `agent-capability-spec.md`, `error-handling.md`, `logging-guidelines.md`,
  `omp-tools-spec.md`, `directory-structure.md` (backend + frontend),
  `README.md:274` comment. `event-pipeline-spec.md:215` already notes removal
  — leave or tighten.
- **D9 (Tests)**: delete CB tests in `tests/python/test_metrics.py`,
  `test_dashboard_metrics.py`, Rust `circuit_breaker.rs` tests (gone with
  file), `local.rs` CB test.
- **Out of scope**: `LlmRateLimiter` (Rust, live); Python `rate_limiter.py`
  `RateLimiter` class (separate — verify live separately, but research
  flagged it as companion type only in proto, the class itself was not
  audited for liveness — leave it, do not delete in this task).

## Acceptance Criteria

- [ ] `grep -rn "circuit_breaker\|CircuitBreaker\|circuitBreaker" .` (excl
      vendor/node_modules/target/.trellis/tasks/archive) returns 0 hits
      except: `event-pipeline-spec.md` removal note, and any
      `rate_limiter.py` `RateLimiter` (separate class) references.
- [ ] `engine.proto` has no CB RPCs/messages/fields.
- [ ] `engine_pb.ts` regenerated (not hand-edited).
- [ ] `circuit_breaker.rs` deleted; `local.rs` has no CB field/methods.
- [ ] `CircuitBreakerPanel.tsx` deleted.
- [ ] `cargo check --workspace` green; `cargo test -p uc-engine` green
      (health test updated to 10 components).
- [ ] `ruff check` green; `pytest tests/python/` green (CB tests removed).
- [ ] `npm run generate` succeeds; `bun test` (dashboard) green; TS typecheck
      green.

## Technical Approach

1. **Proto first**: edit `engine.proto` (D2). Run `cargo check -p uc-grpc`
   to regenerate Rust proto types. Run `npm run generate` in `dashboard/`
   to regenerate `engine_pb.ts`.
2. **Rust engine** (D1): delete file + `local.rs` edits + lib.rs re-export.
   `cargo check -p uc-engine` + `cargo test -p uc-engine`.
3. **Rust gRPC** (D2 impl): delete `dashboard_service.rs` impls/converters.
   `cargo check -p uc-grpc`.
4. **Python** (D3+D4): delete nats_worker stubs, app.py routes/method,
   metrics fields/gauge/alert, `CircuitBreaker` class. `ruff check` +
   `pytest`.
5. **Frontend** (D5+D6): delete types, panel file, hooks/App refs.
   `npm run generate` already done; `bun test` + typecheck.
6. **Specs** (D8): edit 6 spec files + README.
7. **Tests** (D9): delete CB tests.
8. Full verify: cargo check/test workspace, ruff, pytest, dashboard checks.

## Risk

- **Proto breaking change**: removing gRPC RPCs breaks any external client
  calling `GetCircuitBreakerStatus`/`ResetCircuitBreaker`. Both return
  unavailable already, so no functional regression. Pre-1.0, acceptable.
- **Regeneration drift**: if `buf generate` version differs from CI, TS
  output may not match. Use the project's pinned buf (via
  `dashboard/node_modules/.bin/buf`).
- **`RateLimiter` confusion**: the Rust `LlmRateLimiter` and Python
  `RateLimiter` class are LIVE/separate — do not delete them. Only the
  CB-companion `RateLimiterProto`/`RateLimiterInfo` types go.
- **Large diff**: ~40+ file edits. Atomic PR — the feature is self-contained
  and leaving half leaves dangling refs across layers.
