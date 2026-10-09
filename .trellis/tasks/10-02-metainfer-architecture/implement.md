# Implementation and verification

- [x] R1: public TaskStore/TaskService projection and retry regression tests;
  shared status guards, dedicated RPC, OMP integration and recovery coverage.
- [x] R2/R3: cancellation evidence, capacity queue and alias identity; delayed
  writer, lost response, concurrent/cancelled wait and restart tests.
- [x] R5: indexed runtime queries, pooled connections, delivery claims/backoff
  and retention tombstones; large-history and concurrent replay tests.
- [x] R4: CAS recovery/audit API/CLI and Dashboard experiment/resource view.
- [x] R6: workspace preflight and durable hashed artifact distribution.
- [x] R7: actual GPU identity, workload reservations and measurement conditions.
- [x] R8: pinned integration gate and explicit evidence contract. Real GPU
  execution remains an external release-gate input and is not claimed by CPU
  fixtures.
- [x] Update deployment/architecture/operational contracts and README.
- [x] Relevant test suites, independent two-axis review, Trellis check/spec
  update. Two axes ran in parallel (Standards + Spec); every hard finding was
  fixed and every judgement call was measured before being accepted or
  rejected. Announce commit and deliver within existing authorization.

Risk boundaries: server.rs/proto and all generated clients; shared runtime
schema migration; uncertain writer ownership; delivery deduplication. No
cleanup of an uncertain remote writer is permitted without verified evidence.

Commands: cargo test -p uc-grpc; cargo check --workspace; cargo test with
messaging/storage fixtures; pytest tests/python; Bun orchestrator checks;
Dashboard typecheck/build/tests; live PostgreSQL and HTTP fault integration.

## Live verification continuation (2026-10-09)

- [x] Measure real NATS to HTTP SSE transport with three simultaneous clients,
  steady and burst traffic, sequence-level loss/duplicate detection and a
  strict maximum-sample gate. Preserve all samples and rejection checks.
- [x] Deploy unmodified pinned MetaInfer in WSL and run the actual GPU release
  gate. The UI/plugin routes return 200; UC contract routes return 404; gate
  exits 2 without generating pass evidence.
- [ ] R8 real optimization/cancellation/recovery/artifact acceptance. Requires
  a service extension with genuine containment or workspace fencing. The
  upstream deployment and host GPU visibility do not satisfy this criterion.

Evidence and reproducible commands:
[live verification report](../../../docs/live-feedback-and-metainfer-verification.md).
