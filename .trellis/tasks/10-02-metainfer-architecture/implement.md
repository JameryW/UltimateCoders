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

## Contained service continuation (2026-10-11)

- [x] Implement a separately deployed, revision-pinned OCI launcher extension;
  retain upstream task code and expose only contained discovery/create/kill.
- [x] Persist launch intent, immutable container/kernel identity, workspace
  ownership and stop receipts; recover without replaying create/start.
- [x] Verify detached-child containment across launcher-process restart,
  cross-UID workspace challenges and pinned HTTP create/kill with Podman in WSL.
- [x] Fix launch ownership, early validation, duplicate identity claims, short
  process/kill races, host-file observations and native startup escape paths.
- [x] Freeze canonical workspace authorization roots and regress symlink
  retargeting discovered by independent Standards review.
- [x] Document deployment, supported runtime boundaries and measured receipts.
- [x] Deliver this continuation through PR #706, all 13 CI checks and merge
  6a16df1e. Local main was synchronized to the merge.
- [ ] R8: implement contained GPU/model attestation and run the complete real
  optimization, Oracle, rollback, delivery and active-run recovery acceptance.

GPU image provisioning is in progress. The current hardware endpoint returns
503; CPU containment passes do not close R8. This task remains in progress.

## GPU validation continuation (2026-10-12)

- [x] Reproduce control-store alias retargeting on Linux; freeze its canonical
  startup path and add a regression protecting identity/ownership records.
- [ ] Recover the locally installed, checksum-verified GPU job image and run
  actual contained CUDA/Triton probes.
- [ ] Establish actual Ollama coding tool execution and independent file tests;
  text describing a tool invocation is a failed coding check.
- [ ] Complete the R8 GPU/model, optimization and recovery release criteria.
