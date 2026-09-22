# Gateway dispatch live roster test

## Goal

Close #656 残余 1 (gateway-side half): prove `dispatch_gate` /
`placement_target` decide on a roster built purely through real RPCs against
a real `GrpcServer` — not a hand-constructed in-process `WorkerRegistry`.

## Background

- #656 comment (2026-09-18) narrowed the gap: the delivery plane has live
  coverage (`test_nats_live_dispatch.py`, CI step with real `nats-server`),
  but the gateway decision side has none — `crates/uc-grpc/tests/*.rs`
  contains zero references to `placement_target` / `dispatch_gate` /
  `WorkerRegistry` / `register_worker` (re-verified 2026-09-22).
- All unit coverage is pure-function: `place` / `place_with_policy` in
  `placement.rs`, registry-gate tests in `worker_service.rs`.

## Confirmed facts (code evidence)

- `GrpcServer::worker_registry()` is `pub`, returns
  `&Arc<RwLock<WorkerRegistry>>` (`crates/uc-grpc/src/server.rs:2546`) —
  a test can clone the handle before `into_services()` and read the live
  roster afterwards.
- `GrpcEngineClient` exposes `register_worker` (with `contract_version` +
  `projects`), `worker_heartbeat` (with `recent_files` + `per_worker_topic`),
  `deregister_worker` (`crates/uc-grpc/src/client.rs:303-391`).
- Existing harness `crates/uc-grpc/tests/grpc_integration.rs:12-38` serves
  all 4 services (incl. worker_service) on a random port with
  `LocalEngine::new_fallback()`.

## Requirements

1. New integration test file (e.g.
   `crates/uc-grpc/tests/dispatch_live_roster.rs`): boot real server,
   register 2+ workers via RPC with distinct capabilities/scopes/versions,
   heartbeat placement signals via RPC, then assert on the cloned live
   registry handle:
   - `dispatch_gate` refuses a capability nobody holds (stays Pending shape).
   - `dispatch_gate` refuses a review node whose only holders are producers
     (T19 exclusion through the RPC-built roster).
   - `placement_target` targets the affine worker for a constrained node
     and returns `None` (shared fallback) for zero overlap in affinity mode.
   - Contract-version mismatch through RPC is refused / not dispatchable.
2. No hand-built registry in the test: every roster row must arrive via
   `RegisterWorker` / `WorkerHeartbeat` RPCs. Registry handle is read-only
   in assertions.
3. Deterministic: random port, no sleeps beyond the existing 100ms pattern,
   no NATS, no external processes.

## Acceptance

- New test fails before the fix-shape (i.e. it would catch a regression
  that unit tests miss — e.g. RPC field dropped on the floor — verified by
  mutation or by asserting a wire-only field such as `per_worker_topic`).
- `cargo test -p uc-grpc --test dispatch_live_roster` green; full
  `cargo test -p uc-grpc --lib` still green; `cargo fmt --check` clean.

## Out of scope

- Actual NATS publish / JetStream delivery (covered by
  `test_nats_live_dispatch.py`; gateway publish path needs JetStream infra).
- market scheduling (P2-3, still ungrounded), T19 shared-queue race window
  (denied decision D, pending §21).

## Decisions

- S1 (2026-09-22): scope A — assert on the live registry handle built purely
  via RPCs; no NATS publish. Rationale: delivery already covered by
  `test_nats_live_dispatch.py`; gateway publish needs JetStream infra and
  would overlap.

## Open questions

None.
