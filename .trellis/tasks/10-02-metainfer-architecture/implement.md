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
- [x] Deliver through PR #710, all 11 CI checks and merge 45b84854; synchronize main.

Cold-start success is deployment preflight. Full R8 acceptance remains OPEN.

## Single-host harness diagnosis and deferred cross-host acceptance (2026-10-12)

- [x] Record the user's confirmed lack of a second host; keep cross-host
  acceptance incomplete without substituting local containers.
- [x] Replay the unchanged rejected model harness in OCI. All ten cases fail
  because JITFunction.run lacks grid/warmup; original artifacts remain unchanged.
- [x] Replay a diagnostic copy using the documented run(x, y). All ten cases
  pass with zero error; an intentionally wrong candidate fails all ten.
- [x] Inspect the completed review trace: actual tools only list/read files;
  requested review artifact is absent, so upstream gets no file-based feedback.
- [x] Audit all thirteen owned OCI scopes as stopped. Preserve previous audits.
- [ ] Obtain genuine model-generated harness/review artifacts and a successful
  optimization, then protected whole-workflow Oracle/rollback/accepted delivery.
- [ ] Perform actual cross-host artifact verification when a second host exists.

The operator-corrected copy is diagnostic evidence only, not an optimized
candidate or successful model generation. R8 remains OPEN.

## Native files, effective budgets and checkpoint readers (2026-10-12)

- [x] Deliver PR #711 at c12f2e49; preserve its diagnostics and thirteen
  historical stopped-scope receipts.
- [x] Bind bounded context/output budgets to actual HTTP, hardware profile,
  launch and saved conversations, including implicit legacy defaults.
- [x] Add real structured write_file handling, atomic no-follow bounded files,
  actual file_change events and distinct command/message item IDs.
- [x] Reject provider output exhaustion before tools or success events; close
  directory FDs when temporary cleanup fails. Confirm RED then GREEN through
  real HTTP and actual filesystem descriptors.
- [x] Reproduce Windows sharing denial using actual CreateFileW handles; repair
  with five atomic replace attempts and bounded backoff, preserving old bytes
  on persistent error. Both regressions pass.
- [x] Run final full Windows tests (1509 passed, 61 skips), Linux related tests
  (118 passed, 3 skips), Ruff and all seven repository guards.
- [x] Build immutable image 12098cc3 and pass actual model file delivery and
  the HTTP shared-workspace/GPU deployment gate on port 48768.
- [x] Audit all 27 owned scopes on the current WSL boot as stopped, preserving
  thirteen prior-boot records as historical evidence.
- [x] Preserve genuine rejected harnesses/reviews and the latest review timeout.
  No manual artifact correction is counted as model acceptance.
- [x] Complete final independent Spec/Standards evidence reviews and prepare
  the reviewed commit/PR delivery. CI and merge status are recorded by the PR.
- [ ] Complete genuine successful full optimization, protected whole-workflow
  Oracle/rollback/delivery and actual cross-host artifact acceptance.

Actual native file delivery and preflight pass; model harness quality still
fails reference execution or review deadlines. R8 stays OPEN. The user has no
second host and explicitly retained incomplete cross-host acceptance.

## Harness acceptance and review protocol (2026-10-12)

- [x] Preserve PR #712 / 61a65556 as the continuation baseline.
- [x] Reproduce 183s delayed response headers with a 190s caller budget:
  before 180.474s refusal, after 183.704s success. Keep absolute deadlines.
- [x] Improve existing-reference/API/self-test model instructions; retain all
  nine genuine generation/repair/audit outcomes without counting them as R8.
- [x] Detect the 27-case wrong-wrapper false acceptance; retain the compatible
  negative control and the original malformed negative as distinct evidence.
- [x] Run the production UC workflow with protected OCI benchmark measurements.
  Record the setup failure, invalid pinned choice and bounded real optimization.
- [x] Refuse invalid live select/multiselect choices before backend reservation,
  intent and POST. Nine RED/GREEN cases and actual live HTTP refusal pass.
- [x] Repair three Windows coarse-clock test assumptions using deterministic
  clocks, retaining real control functions and precise rate assertions.
- [x] Reproduce explicit FAIL being parsed as PASS by the unchanged pin. Refuse
  ambiguous existing native review files before final completion; preserve bytes
  and cover both real file-tool/shell paths, valid PASS/FAIL and FD boundaries.
- [x] Build separate image7 233de64c; actual GPU/model and native file delivery
  pass. Replay the original failed reviews unchanged inside that image.
- [x] Deploy image7 on local port 48768; final HTTP GPU/workspace gate passes.
- [x] Final Windows Python3.14 suite: 1532 passed, 72 skips, 197.70s. Linux
  related suite: 223 passed, 3 skips, 83.84s. Ruff passes.
- [x] Complete independent Spec/Standards source and evidence reviews; prepare
  the reviewed commit/PR. CI/merge results are recorded by the linked PR.
- [ ] Complete successful genuine optimization, protected candidate Oracle
  acceptance/rejection and accepted delivery. The image6 real attempt reached
  performance review, timed out, stopped all writers, rolled back and released
  its GPU/backend reservations; it did not produce an accepted candidate.
- [ ] Resolve the earlier invalid submission only through evidence-backed
  recovery. Its isolated verification state retains quarantine and reservations.
- [ ] Complete actual two-host artifact acceptance when a second host exists.

## Submission identity recovery (2026-10-13)

- [x] Reproduce identity HTTP 404 on the actual known image7 optimization task.
- [x] Bind the complete UC operation ID and canonical task SHA privately before
  OCI create; expose the identity through the existing operator attach seam.
- [x] Refuse legacy/missing/foreign identities; preserve reservations until
  independent stopped-writer confirmation. Never retrofit a historical binding.
- [x] Pass real OCI/HTTP identity and versioned attach/stop, including metadata
  rewritten inside the container, active capacity preservation and stopped marker.
- [x] Preserve the first operator script's syntax failure, stop its scope and
  recover its verification reservations through the production API.
- [x] Run full Windows tests (1543 passed, 74 skips), final identity tests (53
  passed, 33 skips), Linux related tests (181 passed, 1 skip), Ruff and both
  independent source review axes.
- [x] Settle the image7 3600s-budget trial through operator stop after reproducing
  stale review bytes blocking regeneration; UC rolled back unchanged code and
  released its reservations. No candidate Oracle acceptance occurred.
- [x] Fix the exact pinned generator boundary: preserve only byte-identical
  entry reviews, keep all new/changed/reviewer/generic checks strict.
- [x] Record RED 4 failures/16 passes and GREEN 41 passes; final Windows full
  1544 passes/94 skips, Linux related 201 passes/1 skip, Ruff passes.
- [x] Build image8 877e2b53. Actual model completion preserves the original
  rejected review; generic completion of the same bytes refuses.
- [x] Deploy image8 on 48768; actual GPU/workspace HTTP gate passes. New audit
  observes 88 owned current-boot scopes stopped and 13 earlier-boot historical
  records, without replacing the earlier audits.
- [x] Complete final combined Spec/Evidence and Standards/Evidence reviews:
  both PASS, no actionable findings. Ruff, diff check and all seven guards pass.
- [x] Complete PR #714 delivery: merged at 1e7dc7fa with all eleven CI checks
  successful. R8 remains OPEN.

## Genuine Gemma workflows (2026-10-13)

- [x] Keep the merged PR #714 source and immutable image8; select separate
  explicit installed Gemma profiles with 4096 and 8192 output tokens.
- [x] Complete six protected baseline calls per trial with the original
  reference, valid inner iteration choice `10` and unchanged Oracle thresholds.
- [x] Preserve the model's real FAIL review, malformed file arguments,
  incomplete output and two actual 600s review deadlines. Neither trial
  produced a candidate for the protected Oracle.
- [x] Stop only the identified tasks through the service; independently check
  cgroup receipts, SQLite state, unchanged Git HEAD/kernel and released budgets.
- [x] Record a separate audit: 106 current-boot owned scopes stopped and
  13 historical records. Preserve earlier audits and the unknown quarantine.
- [x] Confirm provider unload by actual model disappearance on the seventeenth
  observation; keep the earlier acknowledged-but-still-loaded report.
- [x] Confirm nine cached contract/hardware/known-identity GETs return HTTP200
  across three services without creating a new probe. Read-only verification
  confirms the previous uncertain quarantine remains unchanged.
- [x] Complete independent Spec/Evidence and Standards/Evidence reviews: PASS,
  no remaining actionable findings. All seven repository guards pass.
- [ ] Complete this evidence PR delivery. R8 remains OPEN.
