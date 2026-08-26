# Worker host self-identification metadata

## Goal

After cross-host scaling (#607), ops cannot tell which machine a
registered worker lives on. Workers already had a free-form
`RegisterWorkerRequest.metadata` field (JSON) that the gateway stored —
but nothing sent it, and `ListWorkers` could not return it.

## Decision (ADR-lite)

- Populate metadata at the Python worker (`hostname`, `pid`, optional
  `compose_project`); keys are a stable mini-contract, value stays opaque
  JSON end-to-end.
- Add `string metadata = 10;` to **WorkerProto** so ListWorkers echoes it.
  This is the first intentional proto change since the "no proto change"
  decision in 08-24 — that decision scoped ScaleWorkers *routing*, not
  observability; an optional additive field is backward compatible with
  all three stacks (prost auto-regen; buf-generated TS re-generated).
- No UI changes: the field is API-surface; consumers render later.

## Requirements

* Rust: registry → proto echo; client accepts optional metadata;
  uc-python binding passes it through.
* Python worker: compact JSON via `NatsWorker._registration_metadata()`.
* Tests: registry round-trip (Rust), metadata shape (Python).

## Out of Scope

* Dashboard/OMP rendering of hostname.
* Heartbeat-carried metadata refresh (metadata is registration-time only).

## Completion Log (2026-08-25)

* Proto: WorkerProto.metadata = 10; TS stubs regenerated for both
  targets (dashboard + orchestrator via npx buf + local protoc-gen-es).
* Rust: to_worker_protos echoes metadata; GrpcEngineClient::register_worker
  gains `metadata: Option<&str>`; PyO3 binding adds optional param;
  dashboard NATS-fallback paths fill empty metadata (+ json parser reads it).
* Python: engine.py wrapper passes metadata through; NatsWorker sends
  `{"hostname":…,"pid":…[,"compose_project":…]}` on gateway registration.
* Tests: registry_proto_carries_registration_metadata (Rust);
  test_registration_metadata_shape (Python). uc-grpc 144+8 green,
  helper suite 31 green, orchestrator gate exit 0 (18 tolerated vendored
  diagnostics unchanged), dashboard vite build clean.
