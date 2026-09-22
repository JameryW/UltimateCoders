# Design: dispatch live roster test

## Shape

New file `crates/uc-grpc/tests/dispatch_live_roster.rs`, following the
`grpc_integration.rs:12-38` harness (random port, `LocalEngine::new_fallback`,
all 4 services). One difference: keep the `GrpcServer` value (or its
`Arc<RwLock<WorkerRegistry>>` clone from `worker_registry()`,
`server.rs:2546`) before `into_services()` consumes it.

## Flow

1. Serve. Connect `GrpcEngineClient`.
2. Register worker `producer` (`code`, `CONTRACT_VERSION`, per-worker
   metadata hostname) and worker `reviewer` (`code` + `review`,
   `UC_CAP_REVIEW` analogue — capability string `review`), both OPEN scope.
3. Heartbeat both with `current_load`, `recent_files`, `per_worker_topic=true`
   via `worker_heartbeat` (`client.rs:351`).
4. Read-only assertions on the cloned registry handle:
   - `dispatch_gate(&["nonexistent-cap"], "", &unconstrained)` →
     `NoCapableWorker`.
   - `dispatch_gate(&["review"], "", &independence excluding producer)` →
     `NoIndependentReviewer` (T19 through the wire).
   - `placement_target` for a node constrained on producer-touched files →
     `Some(producer)`; unconstrained/zero-overlap node in affinity mode →
     `None` (shared fallback).
   - Legacy worker (empty `contract_version` via RPC) is registered but
     absent from `dispatch_candidates`.
5. Wire-fidelity tripwire: at least one assertion must depend on a field
   that only the RPC path can populate (e.g. `per_worker_topic=true` →
   targeted; then a second worker with identical caps but
   `per_worker_topic=false` is never targeted). Verified by temporary
   mutation (drop the field server-side) turning the test red.

## Boundaries

- Registry handle is read-only in the test (no direct `register` /
  `heartbeat_with_signals` calls — that would defeat the purpose).
- No NATS, no sleeps beyond the harness 100ms, no new RPCs, no
  `contract_version` bump, no production code changes expected (test-only;
  if the test exposes a real wire gap, that becomes a separate fix ticket).

## Rollback

Delete the file. No migrations, no config, no envelope changes.
