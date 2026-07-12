# Research: Circuit Breaker Full Cross-Layer Footprint

- **Query**: Map every circuit-breaker reference across Python, Rust, frontend, specs, tests. Classify each as DEAD / STUB / LIVE.
- **Scope**: internal (cross-layer codebase)
- **Date**: 2026-07-12

## Summary Deletion Map (classification per layer)

| Layer | Location | Classification | Action |
|---|---|---|---|
| Python `nats_worker.py` | `_dash_getcircuitbreakerstatus` / `_dash_resetcircuitbreaker` | **STUB** | Delete both methods + call site in `_build_snapshot` |
| Python `dashboard/app.py` | `/dashboard/api/circuit-breaker` route + `_get_circuit_breaker_data` + `/circuit-breaker/reset` route | **STUB** (returns hardcoded unavailable) | Delete route, method, and reset endpoint |
| Python `agent/rate_limiter.py:252` | `CircuitBreaker` class | **DEAD** (never instantiated anywhere) | Delete class |
| Python `dashboard/metrics.py` | `circuit_breaker_state` field + gauge + alert | **LIVE-but-never-triggers** (wired into real logic but source always returns "unknown"/unavailable) | Delete field, gauge, alert config, and all tests |
| Rust `crates/uc-engine/src/circuit_breaker.rs` | `CircuitBreaker` struct + `CircuitBreakerConfig` + `RetryPolicy` | **DEAD** (constructed but `check_circuit_breaker`/`record_llm_success`/`record_llm_failure` never called outside `local.rs` tests) | Delete file + `pub mod` + `pub use` |
| Rust `crates/uc-engine/src/local.rs` | `circuit_breaker` field + `check_circuit_breaker`/`record_llm_*` methods + health component | **DEAD** (methods never called; health() reports it but no request path checks it) | Delete field, methods, health component, tests |
| Rust `crates/uc-grpc/proto/engine.proto` | 2 RPCs + 4 messages + 2 snapshot fields | **STUB** (forwards to Python stub which returns unavailable) | Delete RPCs, messages, fields |
| Rust `crates/uc-grpc/src/dashboard_service.rs` | `get_circuit_breaker_status` / `reset_circuit_breaker` impls + 4 conversion fns | **STUB** | Delete impls + conversion functions |
| Frontend `types/dashboard.ts` | `CircuitBreakerInfo`, `CircuitBreakerData`, `circuit_breaker_state`, `circuit_breaker?` field | **DEAD** (frontend types for stub) | Delete all |
| Frontend `components/panels/CircuitBreakerPanel.tsx` | Full panel (179 lines) | **DEAD** (renders "unavailable" always) | Delete entire file |
| Frontend `components/ui/alert-bar.tsx:39,47` | CB open + rate limiter alerts | **DEAD** (guards on `circuitBreaker.available` which is always false) | Delete the 2 alert blocks |
| Frontend `hooks/useDashboard.ts` + `useDashboardGrpc.ts` | CB state, fetchers, converters | **DEAD** | Delete CB state, imports, fetch calls, converters |
| Frontend `App.tsx` | Panel render, reset handler, fetch calls | **DEAD** | Delete panel block, handler, fetch refs |
| Frontend `components/panels/EventLogPanel.tsx` | `circuit_breaker_reset` event type styling | **DEAD** (no code emits this event) | Delete 2 style lines + Set entry |
| Frontend `components/panels/MetricsPanel.tsx` | `circuit_breaker_state` display + CSV export | **DEAD** (always "unknown") | Delete display row + CSV line |
| Frontend `grpc/engine_pb.ts` | Generated proto types | **STUB** (regenerates from proto) | Regenerate after proto edit; no manual edit needed |
| Specs `.trellis/spec/backend/` | 5 spec files reference CB | **DEAD** (documents removed feature) | Update specs to remove CB |
| Specs `.trellis/spec/frontend/` | 1 spec references `rate_limiter.py` CB | **DEAD** | Update spec |
| Tests `tests/python/test_metrics.py` + `test_dashboard_metrics.py` | ~25 test lines | **DEAD** (tests dead alert logic) | Delete CB-related tests |

---

## Findings

### 1. Python Backend — STUB methods

#### `python/ultimate_coders/nats_worker.py`

| Line | Code | Classification |
|---|---|---|
| 2060-2062 | `_dash_getcircuitbreakerstatus` — returns `{"available": False, "circuit_breaker": {}, "rate_limiter": {}}` | **STUB** |
| 2064-2066 | `_dash_resetcircuitbreaker` — returns `{"success": False, "error": "Circuit breaker removed (sandbox-only mode)"}` | **STUB** |
| 2182-2183 | `_build_snapshot()` calls `await self._dash_getcircuitbreakerstatus({})` | **STUB call site** |
| 2197 | `"circuit_breaker": circuit_breaker` in snapshot dict | **STUB field** |

The NATS dashboard RPC handler `_dash_*` methods are invoked via `nats_dashboard_request("GetCircuitBreakerStatus", ...)` / `nats_dashboard_request("ResetCircuitBreaker", ...)` from the Rust gRPC server (see `dashboard_service.rs:124,143`).

#### `python/ultimate_coders/dashboard/app.py`

| Line | Code | Classification |
|---|---|---|
| 233-238 | `GET /dashboard/api/circuit-breaker` route → calls `_get_circuit_breaker_data()` | **STUB** (REST endpoint) |
| 610-618 | `POST /dashboard/api/circuit-breaker/reset` route → returns hardcoded `{"success": False, "error": "Circuit breaker removed (sandbox-only mode)"}` | **STUB** |
| 1360-1450 | `_get_circuit_breaker_data()` method — checks `hasattr(orch, "circuit_breaker")` but orchestrator never has this attribute (see below) → always returns `available: False` structure | **STUB** |
| 1471 | `_get_full_snapshot()` calls `self._get_circuit_breaker_data(health_data=health)` → includes in snapshot | **STUB call site** |
| 1492-1520 | `_update_system_metrics()` calls `_get_circuit_breaker_data()`, extracts `cb_state` (always "unknown"), passes to `metrics.update_system_state(circuit_breaker_state=cb_state)` | **STUB → feeds dead metrics** |
| 1150 | docstring mentions `circuit_breaker_reset` as an event type — but no code emits this event | **DEAD reference** |

**Key finding**: `_get_circuit_breaker_data()` at line 1397 does `if hasattr(orch, "circuit_breaker") and orch.circuit_breaker is not None:` — the `Orchestrator` class (`python/ultimate_coders/agent/orchestrator.py`) does NOT import `rate_limiter` or define a `circuit_breaker` attribute (confirmed via grep — zero hits). So this branch is never taken; the method always returns `available: False`.

### 2. Python `CircuitBreaker` class — DEAD

#### `python/ultimate_coders/agent/rate_limiter.py:252-394`

The `CircuitBreaker` class is fully implemented (252 lines: `allow_request`, `record_success`, `record_failure`, `state`, `failure_count`, `total_calls`, `total_rejected`, `force_state`, `reset`). However:

- **Zero construction sites**: `grep 'CircuitBreaker(' python/ultimate_coders/` outside `rate_limiter.py` returns **nothing**. No file imports and instantiates this class.
- **Orchestrator does not use it**: `python/ultimate_coders/agent/orchestrator.py` does not import `rate_limiter` or reference `circuit_breaker` / `CircuitBreaker` at all.
- **No agent file uses it**: `grep` across `python/ultimate_coders/agent/` (excluding `rate_limiter.py` itself) for `circuit_breaker` / `CircuitBreaker` returns **nothing**.

**Classification: DEAD** — the class exists but is never instantiated or imported.

### 3. Python `dashboard/metrics.py` — LIVE-but-never-triggers

The metrics infrastructure has real, wired-up logic for circuit breaker state, but because the data source (app.py `_get_circuit_breaker_data`) always returns `available: False` and `cb_state` always resolves to `"unknown"`, the alert condition (`circuit_breaker_state == "open"`) can never be true.

| Line | Code | Classification |
|---|---|---|
| 92 | `SystemMetrics.circuit_breaker_state: str = "unknown"` (dataclass field) | **LIVE-but-dead-source** |
| 112 | `AlertConfig.circuit_breaker_alert: bool = True` (config flag) | **LIVE-but-dead-source** |
| 156 | `self._circuit_breaker_state: str = "unknown"` (aggregator state) | **LIVE-but-dead-source** |
| 214,221-222,232 | `update_system_state(circuit_breaker_state=...)` parameter — called from `app.py:1516` with always-"unknown" value | **LIVE-but-dead-source** |
| 342 | `snapshot()` returns `SystemMetrics(circuit_breaker_state=self._circuit_breaker_state)` | **LIVE-but-dead-source** |
| 407-413 | `check_alerts()` — `if cfg.circuit_breaker_alert and snap.system.circuit_breaker_state == "open":` → creates `Alert("circuit_breaker_open", ...)` | **LIVE-but-never-triggers** |
| 696-700 | Prometheus gauge `uc_circuit_breaker_state` (0=closed, 0.5=half_open, 1=open) | **LIVE-but-always-unknown** (sets to -1 via `state_map.get(circuit_breaker_state, -1)`) |
| 742-750 | `update_system_state()` Prometheus sync — maps "closed"→0, "half_open"→0.5, "open"→1, else→-1 | **LIVE-but-always-unknown** |

**Classification**: The metrics code is **structurally live** (called, wired, tested) but **functionally dead** because the input is always `"unknown"`. The gauge always reads -1, and the alert never fires. These can be deleted alongside the stub, but the `SystemMetrics` dataclass and `MetricsSnapshot` are shared with other live metrics (uptime, rate_limiter_remaining_ratio, cluster_utilization), so only the `circuit_breaker_state` field and `circuit_breaker_alert` config + alert block should be removed.

### 4. Frontend Types — DEAD (types for stub)

#### `dashboard/src/types/dashboard.ts`

| Line | Code | Classification |
|---|---|---|
| 124 | `// ── Circuit Breaker / Rate Limiter ──` section header | **DEAD** |
| 126-136 | `export interface CircuitBreakerInfo` (available, state, failure_count, failure_threshold, total_calls, total_rejected, recovery_timeout_seconds, last_failure, error) | **DEAD** |
| 138-147 | `export interface RateLimiterInfo` (available, rpm_available, tpm_available, active_count, total_requests, remaining_ratio, window_seconds, error) | **DEAD** (companion type) |
| 149-155 | `export interface CircuitBreakerData` (available, circuit_breaker, rate_limiter, engine_circuit_breaker, engine_rate_limiter) | **DEAD** |
| 212 | `SystemMetrics.circuit_breaker_state: string` | **DEAD** (always "unknown") |
| 241 | `DashboardSnapshot.circuit_breaker?: CircuitBreakerData` | **DEAD** |

### 5. Frontend Components — DEAD (never activates)

#### `dashboard/src/components/panels/CircuitBreakerPanel.tsx` (179 lines, entire file)
- **Classification: DEAD** — full panel component. Renders `EmptyState` "Circuit Breaker not available" when `!data.available` (always the case since backend stub returns `available: False`). Has reset button, state badge, failure/threshold/call/rejected metrics, rate limiter gauge — all never shown.
- Line 60: `export const CircuitBreakerPanel = memo(function CircuitBreakerPanel({ data, onReset, stale, embedded })`
- Line 64: `if (!data.available)` → renders unavailable state (always taken)

#### `dashboard/src/components/ui/alert-bar.tsx`
- Line 2: imports `CircuitBreakerData` type
- Line 7: `circuitBreaker: CircuitBreakerData` prop
- Line 18: destructures `circuitBreaker` in props
- Line 38-43: `if (circuitBreaker.available && circuitBreaker.circuit_breaker.state === "open")` → push alert (never true)
- Line 47-55: `if (circuitBreaker.available && circuitBreaker.rate_limiter.available)` → rate limiter alert (never true)
- Line 96: `circuitBreaker` in useMemo deps
- **Classification: DEAD blocks** — the two `if` guards are never entered. The `circuitBreaker` prop and the two alert blocks can be removed. (The AlertBar itself is live — it has other alerts for stale workers, failures, etc.)

#### `dashboard/src/components/panels/MetricsPanel.tsx`
- Line 88-91: displays `Circuit Breaker` label + `s.circuit_breaker_state` value (always "unknown")
- Line 252: `rows.push(\`System,Circuit Breaker,${s.circuit_breaker_state}\`)` in CSV export
- **Classification: DEAD rows** — always shows "unknown". Delete the display row + CSV line.

#### `dashboard/src/components/panels/EventLogPanel.tsx`
- Line 19: `if (type.startsWith("circuit_breaker_reset")) return "text-yellow-500";` (text color)
- Line 34: `if (type.startsWith("circuit_breaker_reset")) return "evt-cb-reset";` (bg class)
- Line 88: `const ERROR_TYPES = new Set(["task_failed", "subtask_failed", "circuit_breaker_reset"]);`
- **Classification: DEAD** — no code emits a `circuit_breaker_reset` event (the reset endpoint at `app.py:611` returns a hardcoded error, never calls `_record_event`). Delete the 3 lines.

#### `dashboard/src/App.tsx`
- Line 12: `import { CircuitBreakerPanel }`
- Line 161-162: `getCircuitBreakerStatus, resetCircuitBreaker: grpcResetCircuitBreaker` from `useDashboardGrpc`
- Line 169: `snapshot.health || snapshot.workers || snapshot.scheduler || snapshot.circuitBreaker` (condition includes CB)
- Line 192: `fetchCircuitBreaker: getCircuitBreakerStatus` in fetchInitial
- Line 306-308: `handleResetCB` async function — calls `grpcResetCircuitBreaker()`, shows toast
- Line 384: `fetchCircuitBreaker: getCircuitBreakerStatus` in retry fetchInitial
- Line 406: `const cbSummary = dashboard.circuitBreaker.available ? dashboard.circuitBreaker.circuit_breaker.state : undefined;` (always undefined)
- Line 431: `circuitBreaker={dashboard.circuitBreaker}` prop to AlertBar
- Line 555-566: `<ErrorBoundary name="Circuit Breaker">` wrapping `<SidebarPanel title="Circuit Breaker">` containing `<CircuitBreakerPanel>`
- **Classification: DEAD** — all references. Delete the import, the panel block, the handler, the fetch refs, the `cbSummary` line, and the AlertBar prop.

#### `dashboard/src/hooks/useDashboard.ts`
- Line 10: imports `CircuitBreakerData`
- Line 54-72: `useState<CircuitBreakerData>` initial state (hardcoded unavailable)
- Line 92: `circuitBreaker?: CircuitBreakerData` in handleSnapshot param type
- Line 101: `if (data.circuitBreaker?.available) setCircuitBreaker(data.circuitBreaker)` (never true)
- Line 318: `fetchCircuitBreaker?: () => Promise<CircuitBreakerData>` in fetchInitial opts
- Line 329: `opts?.fetchCircuitBreaker?.()...` call
- Line 410: `circuitBreaker` in return value
- **Classification: DEAD** — delete the import, state, the snapshot field, the fetch option, and the return value.

#### `dashboard/src/hooks/useDashboardGrpc.ts`
- Line 8-9,20,25,43-44: imports from `engine_pb.ts` and `types/dashboard.ts`
- Line 127-151: `grpcCircuitBreakerToDashboard()` converter function
- Line 153-163: `grpcCircuitBreakerStatusToDashboard()` converter function
- Line 273: `circuit_breaker_state: m.circuitBreakerState` in metrics conversion
- Line 292: `circuit_breaker_state: "unknown"` in fallback
- Line 318,414,421,482,496-497: `circuitBreaker?` in snapshot conversion types + assignments
- Line 564-568: `getCircuitBreakerStatus` callback (calls gRPC `client.getCircuitBreakerStatus`)
- Line 571-576: `resetCircuitBreaker` callback (calls gRPC `client.resetCircuitBreaker`)
- Line 620-621: returns `getCircuitBreakerStatus, resetCircuitBreaker`
- **Classification: DEAD** — delete all imports, converters, callbacks, and snapshot field mappings.

#### `dashboard/src/grpc/engine_pb.ts` (generated)
- Lines 1899-2046, 2277-2279, 2536-2538, 2948-2961: generated proto types for `GetCircuitBreakerStatusRequest`, `CircuitBreakerStatusResponse`, `CircuitBreakerProto`, `ResetCircuitBreakerRequest`, `ResetCircuitBreakerResponse`, `DashboardSnapshot.circuit_breaker` field, `SystemMetrics.circuit_breaker_state` field, `DashboardService.getCircuitBreakerStatus`/`resetCircuitBreaker` RPCs.
- **Classification: STUB (generated)** — do NOT manually edit. Regenerate from `engine.proto` after deleting the proto definitions.

### 6. Rust Layer

#### `crates/uc-engine/src/circuit_breaker.rs` (445 lines, entire file)
- `CircuitState` enum (Closed/Open/HalfOpen)
- `CircuitBreaker` struct (state, failure_count, success_count, failure_threshold, success_threshold, reset_timeout, last_failure_time, total_calls, total_rejected)
- `CircuitBreakerConfig` struct
- `RetryPolicy` struct (max_retries, base_delay, max_delay, jitter, `delay_for_attempt()`)
- 10 unit tests
- **Classification: DEAD** — `CircuitBreaker` is constructed by `LocalEngine` (3 sites: `local.rs:137,214,275`) but the methods `check_circuit_breaker()`, `record_llm_success()`, `record_llm_failure()` are **never called** by any production code path. Grep across `crates/uc-grpc-server/`, `crates/uc-grpc/`, `crates/uc-python/` for these method names returns zero hits outside `local.rs` and `circuit_breaker.rs` tests. The gRPC server constructs the engine and serves health (which reports CB state) but no request-execution path checks the CB.
- **Note**: `RetryPolicy` is `pub use`d from `uc-engine/src/lib.rs:24` and is a separate concern (retry backoff, not circuit breaking). Check if `RetryPolicy` is used elsewhere before deleting it.

#### `crates/uc-engine/src/local.rs`
| Line | Code | Classification |
|---|---|---|
| 11 | module doc comment references `CircuitBreaker` | **DEAD** |
| 21 | `use crate::circuit_breaker::CircuitBreaker;` | **DEAD** |
| 59-60 | `circuit_breaker: Arc<CircuitBreaker>` field on `LocalEngine` | **DEAD** (constructed but methods unused) |
| 137,214,275 | `let circuit_breaker = Arc::new(CircuitBreaker::with_defaults());` in 3 constructors | **DEAD** |
| 156,224,285 | `circuit_breaker,` field assignment | **DEAD** |
| 328-330 | `pub fn circuit_breaker(&self) -> &Arc<CircuitBreaker>` accessor | **DEAD** (never called) |
| 400-412 | `check_circuit_breaker()`, `record_llm_success()`, `record_llm_failure()` methods | **DEAD** (never called outside tests) |
| 653-666 | `health()` pushes a `"circuit_breaker"` ComponentHealth | **DEAD** (reports state of unused CB) |
| 941-942 | test comment + assertion `health.components.len() == 11` (includes CB) | **DEAD** (update count to 10) |
| 982 | `assert_eq!(health.components[9].name, "circuit_breaker")` | **DEAD** (test) |
| 1301-1318 | `local_engine_circuit_breaker` test | **DEAD** (test) |

#### `crates/uc-engine/src/lib.rs`
| Line | Code | Classification |
|---|---|---|
| 7 | `pub mod circuit_breaker;` | **DEAD** |
| 24 | `pub use circuit_breaker::{CircuitBreaker, CircuitBreakerConfig, CircuitState, RetryPolicy};` | **DEAD** (CB part); **CHECK** RetryPolicy usage |

#### `crates/uc-grpc/proto/engine.proto`
| Line | Code | Classification |
|---|---|---|
| 49 | `rpc GetCircuitBreakerStatus(GetCircuitBreakerStatusRequest) returns (CircuitBreakerStatusResponse);` | **STUB** |
| 50 | `rpc ResetCircuitBreaker(ResetCircuitBreakerRequest) returns (ResetCircuitBreakerResponse);` | **STUB** |
| 532 | `message GetCircuitBreakerStatusRequest {}` | **STUB** |
| 534-538 | `message CircuitBreakerStatusResponse` | **STUB** |
| 540-546 | `message CircuitBreakerProto` | **STUB** |
| 548-553 | `message RateLimiterProto` | **STUB** (companion, only used by CB response) |
| 555-561 | `message ResetCircuitBreakerRequest` + `ResetCircuitBreakerResponse` | **STUB** |
| 611 | `optional CircuitBreakerStatusResponse circuit_breaker = 6;` in `DashboardSnapshot` | **STUB** |
| 669 | `string circuit_breaker_state = 2;` in `SystemMetrics` | **STUB** (always "unknown") |

#### `crates/uc-grpc/src/dashboard_service.rs`
| Line | Code | Classification |
|---|---|---|
| 119-136 | `get_circuit_breaker_status()` impl — calls NATS `GetCircuitBreakerStatus`, converts via `json_to_circuit_breaker_status_response`; on error returns `available: false, circuit_breaker: None, rate_limiter: None` | **STUB** |
| 138-146 | `reset_circuit_breaker()` impl — calls NATS `ResetCircuitBreaker`, converts via `json_to_reset_circuit_breaker_response` | **STUB** |
| 538-546 | `json_to_cb_proto()` — converts JSON to `CircuitBreakerProto` | **STUB** |
| 548-555 | `json_to_rl_proto()` — converts JSON to `RateLimiterProto` | **STUB** |
| 557-563 | `json_to_circuit_breaker_status_response()` | **STUB** |
| 565-571 | `json_to_reset_circuit_breaker_response()` | **STUB** |
| 782 | `circuit_breaker_state: json_str(v, "circuit_breaker_state").to_string()` in `json_to_system_metrics` | **STUB** |
| 828-830 | `circuit_breaker: v.get("circuit_breaker").map(json_to_circuit_breaker_status_response)` in `json_to_dashboard_snapshot` | **STUB** |
| 960 | `circuit_breaker: None` in test snapshot builder | **STUB** |

### 7. Specs / Docs

| File | Line(s) | Content | Classification |
|---|---|---|---|
| `.trellis/spec/backend/dashboard-spec.md` | 30,38,50,103,131,216-220,225,239,320,348,383,389,391,398,400,414,418 | Full CB API spec, data shape, reset endpoint, fallback behavior | **DEAD** (documents removed feature) |
| `.trellis/spec/backend/agent-capability-spec.md` | 77,124-129,159,202 | CB integration in `_execute_with_llm`, behavior table | **DEAD** (orchestrator has no CB) |
| `.trellis/spec/backend/error-handling.md` | 103,226,298 | CB integration in error handling, component health row | **DEAD** |
| `.trellis/spec/backend/logging-guidelines.md` | 51,53,127 | CB state-change warn logging example | **DEAD** |
| `.trellis/spec/backend/omp-tools-spec.md` | 307 | `uc_circuit_breaker` tool row | **DEAD** |
| `.trellis/spec/backend/event-pipeline-spec.md` | 215 | "Orchestrator: no ... circuit_breaker" (already notes removal!) | **LIVE** (confirms removal) |
| `.trellis/spec/backend/directory-structure.md` | 40 | `circuit_breaker.rs` file listing | **DEAD** |
| `.trellis/spec/frontend/directory-structure.md` | 28 | `rate_limiter.py # ... circuit breaker` | **DEAD** |
| `README.md` | 274 | `scheduler.ts # DAG builder, wave splitter, circuit breaker` | **DEAD** (comment, not code) |
| `CLAUDE.md` | — | no CB references | N/A |

### 8. Tests

#### `tests/python/test_metrics.py`
- Line 207: `circuit_breaker_state="open"` in `SystemMetrics` construction
- Line 213: `assert snap.system.circuit_breaker_state == "open"`
- **Classification: DEAD** — tests the `SystemMetrics` field that always reads "unknown" in production.

#### `tests/python/test_dashboard_metrics.py`
| Line | Content | Classification |
|---|---|---|
| 50 | `agg._circuit_breaker_state = "unknown"` | **DEAD** |
| 74 | `assert cfg.circuit_breaker_alert is True` | **DEAD** |
| 98 | `circuit_breaker_alert=False` in `AlertConfig` construction | **DEAD** |
| 103 | `assert cfg.circuit_breaker_alert is False` | **DEAD** |
| 339 | `circuit_breaker_state="open"` in `SystemMetrics` | **DEAD** |
| 344-366 | `test_circuit_breaker_state_mapping` — tests Prometheus gauge mapping (closed→0, half_open→0.5, open→1, unknown→-1) | **DEAD** |
| 439-445 | `test_circuit_breaker_open_alert` — tests `Alert("circuit_breaker_open")` fires when state=="open" | **DEAD** |

#### Rust tests (in `circuit_breaker.rs` `#[cfg(test)]` mod, lines 274-444)
- 8 tests: `circuit_breaker_starts_closed`, `circuit_breaker_opens_after_threshold`, `circuit_breaker_rejects_when_open`, `circuit_breaker_half_open_after_timeout`, `circuit_breaker_closes_after_success_in_half_open`, `circuit_breaker_reopens_on_half_open_failure`, `circuit_breaker_success_resets_failure_count`, `force_state_for_testing`
- **Classification: DEAD** — tests the dead `CircuitBreaker` struct.

#### Rust tests in `local.rs` (lines 1301-1318, 941-982)
- `local_engine_circuit_breaker` test + health component count assertions
- **Classification: DEAD**

---

## Caveats / Not Found

1. **`RetryPolicy` coupling**: The Rust `RetryPolicy` struct lives in `circuit_breaker.rs:232-272` and is `pub use`d from `lib.rs:24`. It is conceptually separate from the CB (it's about backoff, not circuit breaking). Before deleting `circuit_breaker.rs`, grep for `RetryPolicy` usage across `crates/` and `python/` to determine if it needs to be relocated rather than deleted. (Initial grep shows it's only defined + tested in `circuit_breaker.rs`, but the `pub use` makes it part of the public API — external consumers may depend on it.)

2. **`uc-python` PyO3 binding**: The Rust `uc-python` crate exposes `LocalEngine` to Python. If the Python side calls `engine.circuit_breaker()` or `engine.check_circuit_breaker()` via the PyO3 binding, those would be live call sites. Grep of `crates/uc-python/` returned no hits for these method names, but a Python-side `engine.circuit_breaker` attribute access (via PyO3 auto-binding) would not show in Rust grep. Worth a Python-side grep of `engine.circuit_breaker` / `engine.check_circuit_breaker` to be fully certain. (The `dashboard/app.py:1397` check is `hasattr(orch, "circuit_breaker")` on the **Orchestrator**, not the engine — and the Orchestrator does not have this attribute.)

3. **`RateLimiterProto` and `RateLimiterInfo`**: The frontend `RateLimiterInfo` type and the proto `RateLimiterProto` are companions to the CB (they're part of `CircuitBreakerStatusResponse`). They should be deleted together with the CB. The Rust-side `RateLimiter` in `local.rs` (the `rate_limiter` field + `rate_limiter.rs` module) is a **separate, live** component (used by `acquire_rate_limit()` / `release_rate_limit()` — verify call sites before touching).

4. **Prometheus gauge `uc_circuit_breaker_state`**: If any external monitoring (Grafana dashboards, alerting rules) scrapes this metric, removing it will break those. No such config found in-repo, but external consumers may exist.

5. **Generated proto code**: `dashboard/src/grpc/engine_pb.ts` is generated. After editing `engine.proto`, run the codegen (likely `buf generate` or the project's proto generation script) to regenerate. Do not hand-edit the `.ts` file.
