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
- [x] Recover the locally installed, checksum-verified GPU job image and run
  actual contained CUDA/Triton probes.
- [x] Establish actual Ollama coding tool execution and independent file tests;
  text describing a tool invocation is a failed coding check.
- [ ] Complete the R8 GPU/model, optimization and recovery release criteria.

PR #707 merged at 48410a6e with 11 passing CI checks. Runtime preflight ran
actual CUDA/Triton kernels; the native Ollama streaming probe created code via
real tools and passed an independent second-container assertion. Neither closes
R8. Portable evidence is in gpu-verification.json.

## Candidate publication continuation (2026-10-12)

- [x] Reproduce the missing repository publication at the public Launcher seam.
- [x] Publish ranked measured evolve-kernel code atomically inside owned OCI.
- [x] Negotiate image capability and validate final receipt/checksum after stop.
- [x] Refuse remote worker delegation outside the contained writer scope.
- [x] Verify image packaging and actual publication in a controlled OCI fixture;
  this fixture does not count as a real MetaInfer optimization.
- [x] Complete independent Spec/Standards reviews and address all findings.
- [x] Run full Windows Python regressions: 1472 passed, 40 platform skips;
  Linux focused checks: 75 passed; Windows delivery/cancellation checks: 42 passed.
- [x] Deliver through PR #708, all 11 CI checks and merge 6db2189e.

Real Qwen optimization failed to produce a harness. The first Gemma attempt
timed out and was stopped with proof. A streaming Gemma run is generating and
reviewing its correctness harness. Full optimization has not passed. Hardware
HTTP still returns 503; attestation, Oracle GPU outcomes and delivery remain open.

## Hardware attestation continuation (2026-10-12)

- [x] Stop the streaming trial after repeated out-of-contract harness reviews;
  retain the failed outcome and full stopped-writer receipt.
- [x] Implement explicit image-owned CUDA/Triton and Ollama nonce attestation,
  native streaming client and before/after digest checks.
- [x] Measure actual kernels/model in OCI with image/code/runtime identities
  and stopped scope. This is preflight only.
- [x] Verify active-writer cache reuse, real native CLI, HTTP GPU deployment
  gate, independent Spec/Standards review and regressions.
- [x] Deliver the hardware continuation through PR #709, 13 passing CI checks
  and merge 87abce56; synchronize local main.
- [ ] Complete R8 optimization/Oracle/rollback/artifacts; actual two-host
  verification also requires an available second host.

## Actual GPU recovery and cancellation evidence (2026-10-12)

- [x] Restart the HTTP daemon during the actual attested optimization; preserve
  its container/PID, hardware evidence and record census with no replay.
- [x] Cancel the 900s optimization deadline with a full cgroup stop receipt;
  compare repository bytes directly and preserve the failed terminal outcome.
- [x] Measure unchanged code with a read-only protected GPU harness and the
  production Oracle. Correctness passed; noise, clock drift, residual utilization
  and lack of improvement correctly prevented acceptance.
- [ ] Complete a successful real optimization and protected whole-workflow
  acceptance/rollback, accepted patch delivery and actual two-host download.

No accepted candidate exists from this trial. The independent Oracle diagnostic
ran after cancellation and does not establish the complete remote workflow.

## Cold-start deadline repair (2026-10-12)

- [x] Reproduce actual WSL nvidia-smi timeout at 2.010s; preserve the 503.
- [x] Bound device observation at 5s, hardware HTTP at 10s and the contained
  workspace challenge at 30s. Identity failure still prevents launch.
- [x] Reproduce client failures with real HTTP responses after 5.3s; retain
  nonce files through the workspace response. Confirm 2.3s device acceptance
  and refusal of a stalled 6s query at the 5s service deadline.
- [x] Repeat actual GPU deployment gate successfully. Audit all eleven owned
  writer scopes as stopped, including the earlier timed-out workspace probe.
- [x] Repair both independent review findings: absolute HTTP operation deadlines
  and persistent unknown-probe lifecycle, identity negotiation, exclusive claim,
  nonce retention, replay refusal and versioned stop reconciliation.
- [x] Exercise a 0.1s client deadline against real OCI: retain nonce and record,
  refuse replay, then reconcile with a matching cgroup stop receipt.
- [x] Complete final full/focused regressions and both independent review axes.
- [ ] Deliver the deadline repair and recovery evidence through passing CI and merge.

Cold-start success is deployment preflight. Full R8 acceptance remains OPEN.
