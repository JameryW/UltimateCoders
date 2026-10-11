# Verification and two-axis review evidence

Baseline `2b22e9adadb3def4f3ffb34e7f6afa8075524f48`. All R1-R8 code was already
in the working tree; this pass is the closing gate named by implement.md.

## Test suites (all green)

| Suite | Result |
|-------|--------|
| `cargo test --workspace` | 357 passed, 0 failed |
| `cargo test -p uc-engine -p uc-grpc --no-default-features` | pass |
| `cargo test -p uc-engine --features indexing` | pass (18 ignored, infra-gated) |
| `cargo test -p uc-grpc --all-features` | pass (4 ignored: NATS broker / PostgreSQL gated) |
| `cargo check --workspace --all-targets` | pass |
| `cargo fmt --all -- --check` | pass |
| `pytest tests/python` | 1384 passed, 11 skipped |
| `ruff check scripts/ python/ tests/python/` | pass |
| Dashboard `tsc -p tsconfig.app.json` + `tsconfig.node.json` | pass |
| Dashboard `vite build` | pass |
| `check-line-endings`, `check-tasks-refs`, `check-readme-ci-table`, `check-workflow-inputs`, `check-journal-ledger` | pass |
| `check-spec-refs --audit` | pass, 112 ok / 9 stale / 9 ambiguous / 0 structural |

### The integration suites the PR's own checks skip

`ci-rust.yml`'s `storage-integration` job is `main`-push-only, so it was
**skipped** on PR #702 and none of the `#[ignore]` integration tests ran against
the branch there. They did run on the merge to `main` (Rust CI run
`37174488192`), which is where the live-fixture half of the verification comes
from:

- `storage integration tests` -- success, 8 ignored tests executed over the
  workspace (live NATS replay, deployed-Gateway contracts, TiKV), every
  `test result: ok`.
- `postgres integration tests` -- success, the `--ignored` PostgreSQL suite
  (`cargo test -p uc-engine --features storage -- --ignored --test-threads=1 postgres`).

That covers the "cargo test with messaging/storage fixtures" and "live
PostgreSQL" lines of the task's verification commands. It is still worth
re-running them on any branch that touches `crates/**` before merging, because
a PR cannot see them.

## Independent two-axis review

Ran as two parallel sub-agents on the baseline diff plus the 12 untracked files.
Both were report-only; neither could edit.

### Hard findings, all fixed

1. **Silent exception swallowing** (`dashboard/app.py`) -- five `except Exception:
   return JSONResponse(...503)` sites logged nothing. Contradicts
   `error-handling.md` Forbidden Pattern #3. Fixed with `_unavailable()`, which
   logs with `exc_info=True` and also removes the copy-pasted 503 shape.
2. **Guard pins moved by the change** -- `metainfer-release-gate.yml` is a new
   workflow, so the README CI table, the prose count word and the workflow-inputs
   summary all had to move in the same change. Done in both READMEs.
3. **Dead parameter** `full_snapshot_is_stale(&self, update, _confirmed)`. The
   tests assert `true` and `false` give the same answer, so the parameter was
   weight without meaning, and its doc comment was a leftover from a de-dup
   function. Removed; doc replaced with the real fencing rule.
4. **`InferencePanel.tsx`** duplicated the auth-header construction in `request()`
   and `download()`. Extracted `authHeaders()`.

### Judgement calls, resolved by measurement rather than by argument

| Finding | Measurement | Verdict |
|---------|-------------|---------|
| Use `Optional[X]` / `List[X]` / `Dict[...]` per `type-safety.md` | `X \| None` 405 vs `Optional[X]` 3 (2 carry `# noqa: UP045`); `list[` 306 vs `List[` 0; `dict` 148 vs `Dict[` 0 | **Rejected, and the spec corrected.** The spec stated the opposite of the tree. Rewriting 405 annotations to match 3 would fight ruff's UP006/UP045. |
| `@dataclass` for `QuiescenceProof` etc. per `component-guidelines.md` | `TypedDict` 0 vs `@dataclass` 71 -- the rule is real | **Rejected on scope.** That rule governs UC's own domain types. `service_contract.py` decodes third-party JSON whose schema is owned by the service under `contract_version`; wrapping it in a dataclass turns a contract verdict into an `AttributeError`. Recorded as gotcha 7a. |
| `retryable` class attribute is ad-hoc | `runner.py:439` consumes it via `getattr(exc, "retryable", ...)` | **Not dead.** It is the R3 channel that keeps a resource wait from spending an execution retry. Recorded as convention 7b. |
| Route `InferencePanel` through `api/endpoints.ts` | that module is gRPC-Web, File Browser only; the panel talks REST to the Dashboard API | **Rejected.** Mixing protocols would be worse than the duplication. |
| Move `Experiment`/`Job`/`Resource` to `types/dashboard.ts` | the panel is the only consumer | **Rejected** as Speculative Generality. |

### Fix to a test that was asserting the wrong layer

`test_inference_architecture.py` expected `match="integrity"` for a tampered
`sha256` field. `ArtifactStore.read()` raises "Invalid artifact metadata" for a
self-contradictory descriptor and "Artifact integrity check failed" for bytes
that do not match. The two are different failures on purpose. The test now pins
BOTH layers instead of conflating them.

### One guard assertion narrowed to its stated property

`test_check_line_endings.py` compared the whole index blob against the working
tree for three files. That over-approximates "these files are LF-only" into
"these files never change" -- the exact fragility its own docstring rejects when
it refuses to pin sizes. It broke on a whitespace-only reformat. Both halves now
assert `b"\r" not in ...`, on the working tree and on the index blob, which is
the property the ticket actually normalized.

## Out of scope, still out of scope

No MetaInfer fork, no automatic clearing of uncertain writers, no Oracle
threshold change, `UC_GRAPH_SHADOW` still default off.

## R7 closed on real hardware; R8 is not

After the merge, R7's identity acceptance was exercised against the **actual**
device rather than a fixture. The host has a real NVIDIA GPU and
`capture_environment` read it through `nvidia-smi` unmocked -- RTX 4060 Laptop,
UUID `GPU-0e9353b0-7678-a5ab-eb56-083ecede327f`, driver 617.14, 8188 MiB,
nvcc 12.6. Two acceptance points closed there: a mismatched `gpu_uuid` /
`gpu_model` / `gpu_driver` is rejected, and `require_gpu=true` with no
observable device is rejected. Full table in
`docs/metainfer-reliability-verification.md`.

At the earlier inspection, R8 stayed **unclaimed** for this reason:
`nvidia-smi` is present, `metainfer` is not. No MetaInfer process, package or
checkout exists on the host, so `scripts/verify-metainfer-release.py` could not
be run at all. Worse, pointing it at stock upstream would *correctly* fail. That
used to be an inference; it is now checked against the real pinned source
(`HuangPuStar/MetaInfer` @ `b3f6505a11ab`, 2026-09-29), checked out and searched:
`/api/uc` and `quiescence`/`workspace_probe`/`contract_version`/`uc-metainfer`
are all **0 occurrences**, and the route table carries no UC contract route.

The other half of the adapter surface *is* compatible -- `/api/sys-shell/
task-types`, `/api/sys-shell/task-types/{type}/schema` and the sys-shell
CRUD/control routes exist upstream and are covered there by the upstream
`test_app_core` server suite. So the gap is narrow: **only the safety contract
is missing** -- the four `/api/uc/*` endpoints that gate a mutating job behind
workspace sharing, all-writer quiescence and GPU identity.

The remaining input is therefore a MetaInfer deployment carrying the UC service
extension at pinned revision `b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890`. Until
that exists, no MetaInfer acceptance, cancellation, recovery or artifact
delivery result is claimed, and the CPU fixtures are not a substitute.

## Live continuation (2026-10-09)

The earlier lack of a package/process/checkout is resolved. The unmodified
pinned upstream now runs in WSL at http://127.0.0.1:48765. Its HTML and plugin
discovery endpoints returned 200; contract and hardware endpoints returned
404. The actual release gate returned 2 with the named workspace/stop-contract
refusal and produced no pass evidence. This proves the deployment blocker
through live HTTP; R8 optimization and GPU acceptance remain open.

Real NATS-to-HTTP SSE verification closes the historical transport measurement:
three clients each received 300 events in steady and burst runs, with no loss
or duplicates. Maximum samples were 6.3938ms and 55.7119ms respectively. All
1800 samples, actual conditions and the contract refusal are retained in
[live-verification.json](live-verification.json). Browser rendering and
cross-host latency were outside the measurement.

The focused gate regressions and real negative checks ensure that missing
events, duplicates, slow outliers and unreachable APIs cannot become passes.

### Continuation Standards review

Standards: no remaining documented violations or actionable baseline smells
found. The revised cleanup adds explicit deadlines for consumer cancellation
and NATS closure. The transport test uses injected HTTP/NATS fixtures and
verifies fragmented frames, duplicates, closure and stalled-close failure.
The report preserves the measurement boundary and leaves R8 unverified.

### Continuation Spec review

No remaining Spec findings in the staged continuation. The initial review
found P2: cancelling consumers and closing the NATS publisher had no deadline,
so an unavailable Dashboard could leave the gate hanging. Both operations now
have explicit timeout bounds, and a deterministic stalled-close regression
verifies the gate itself finishes and closes HTTP streams. The strict latency
verdict, sequence evidence, measurement boundary and deployment refusal remain
consistent with the specifications. No scope creep found.

R8 remains open: the missing service extension prevents real optimization,
cancellation, recovery and artifact-delivery acceptance. Stock upstream startup
and GPU visibility do not satisfy it.

Final review totals: Standards 0 remaining findings; Spec 0 remaining findings
(1 P2 fixed). The two axes ran independently against the staged continuation
from baseline 9c70e329f65eef5f1fcda02932abb0efa6836ae6.

### Continuation final checks

- Full Python suite: 1443 passed, 11 skipped, 2 dependency deprecation warnings.
- Final reference/line-ending guard tests: 64 passed. Census: 2001 tracked
  files, 1992 text; 130 line references, 297 path mentions, 865 task references.
- Focused SSE/Dashboard tests: 26 passed, including all 14 latency-gate cases.
- Ruff and format checks, workflow input coverage and Codex workflow wiring:
  passed.
- Final live transport smoke: 30 events to each of 3 clients, zero loss or
  duplicates, maximum 7.3919ms, exit 0. Unavailable Dashboard/broker checks
  returned 3 without measurement evidence; strict threshold returned 1.
- Actual pinned MetaInfer release gate: exit 2, contract refusal, no pass
  evidence. R8 remains in progress.

## Contained service continuation (2026-10-11)

Standards: 0 remaining findings. The independent review found P2: a configured
workspace-root symlink could be retargeted after startup. ServiceConfig now
freezes the canonical roots, and a Linux RED/GREEN regression verifies the new
target remains unauthorized. Review confirmed the fix and directory boundaries.

Spec: 0 remaining findings. Review confirmed all lifecycle, cross-UID and HTTP
receipts match the raw measurements and their stop-proof hashes. The live report
now states the exact launcher-process restart boundary and leaves R8 OPEN.
Native upstream startup, unsafe host readers and writer ownership issues found
earlier were fixed and reviewed on both axes.

Final checks:

- Windows full Python suite: 1463 passed, 16 skipped in 190.23s; two dependency
  deprecations and one local pytest-cache warning.
- Linux contained-service regressions: 25 passed; one dependency deprecation.
- Real final-code OCI run: detached writer survived launcher-process exit,
  fresh-process recovery observed it, and cancellation stopped it before a
  durable cgroup receipt. Cross-UID HTTP challenge and pinned create/kill passed.
- Ruff lint passed for python/tests/scripts; formatting passed for changed
  Python files. Repository-wide formatting reports ten pre-existing files;
  those are outside this continuation and no format-only edits were made.
- Line endings: 2013 tracked files, 2004 text. Spec references: 130 line refs,
  297 mentions. Task context: 865 valid references. Workflow inputs, Codex issue
  wiring and README CI table checks passed.

Receipts and actual identities: service-verification.json. GPU/model hardware
attestation currently returns 503. GPU optimization, Oracle acceptance/rejection,
rollback, delivery and active HTTP run recovery remain unverified under R8.

## Control-store continuation (2026-10-12)

The Linux regression first failed against the merged implementation: after
retargeting the control-store alias to the workspace, ContainedJobs initialized
private records there. ServiceConfig now fixes the canonical store path when
the configuration is created. Repeating the regression preserves the private
identity/record directory, and the full Linux service suite passed (26 tests).

Independent Standards and Spec reviews both passed with no findings. Ruff lint
and formatting passed for both changed Python files. The Windows focused suite
passed (20 tests, 6 Linux-only cases skipped); reference and line-ending guards
passed. PR #706 is merged at
6a16df1e with all 13 CI checks passed; CPU containment delivery is complete.
GPU image provisioning and local Ollama coding verification continue separately.
R8 remains OPEN; neither provisioned dependencies nor model-generated claims
of tool execution constitute optimization acceptance.

## Hardware attestation continuation (2026-10-12)

Review baseline: 6db2189e0214203f23d7823732ab90213c38fdb3 (merged PR #708).
Both independent axes passed. Spec found a P2 command-batch deadline gap:
the native client now checks the remaining turn budget before every command.
Standards found that HTTP idle timeouts allowed continuous trickle responses
to exceed the operation deadline. Requests now run in a bounded disposable
process covering DNS, headers and body; real HTTP trickle regressions passed.
The cache restart/legacy-owner and launch-time identity drift cases were added.

- Windows full Python suite: 1483 passed, 46 platform skips in 174.99s;
  two dependency deprecation warnings.
- Linux focused service, native protocol and publication suite: 66 passed
  in 14.49s. Ruff and changed-file formatting passed (16 Python files).
- Final immutable OCI image f2d47104f71e50bd62d8c9910c5e351d3a7ef1db63c2e489f3316f7bdd20d411:
  actual Torch/Triton cases at 33, 4096 and 65536 elements matched exactly;
  a random model nonce matched under the pinned Ollama digest.
- A fresh launcher reused the same hardware evidence while an independent
  writer continued changing its file; no new hardware probe was launched.
- Production native CLI executed two actual tools and created smoke.py;
  a second OCI job independently checked positive, negative and fractional sums.
- Actual HTTP deployment at localhost:48768 passed the public --require-gpu
  gate with workspace, hardware and model receipts. All probe/code writers
  stopped with durable cgroup evidence.

Portable identities and receipts: gpu-verification.json. These are deployment
preflights; R8 remains OPEN for real optimization, protected Oracle accept/reject,
rollback, commit/artifact delivery and two-host verification. The older streaming
trial was stopped after repeated harness rejection; independent Git-byte
comparison confirmed its repository remained unchanged.

## Actual optimization recovery and negative GPU evidence (2026-10-12)

PR #709 merged at 87abce5661efe4188a967b098b52df624d8453f6 with all 13 CI
checks passed. Local main was synchronized. The actual attested task
uc-attested-documented-gpu-optim-b5372b2f survived HTTP daemon termination and
restart: container 16650c7eeb6222953f3192596f464dd7559b0cbe0b0c93c702f3b50b34f4c974,
PID 517663 and cached hardware evidence remained unchanged, with no new jobs or
hardware probe. The observer was paused only during daemon replacement and then
resumed against the same identity; this is service recovery, not Worker recovery.

The model-generated harness was reviewed and regenerated but did not finish
within 900s. Cancellation produced stopped scope evidence
ddeeae66b0188a189bfda1e7683150f298a1db401d5af235ed48b2360faceeee.
The repository SHA stayed 87c37016af10e1959103a34bcbcec75e306f2975848f1b5f33282807eb798e67.
No optimized candidate, acceptance or delivery is claimed.

An independent read-only harness then measured the unchanged reference and
repository kernel on the actual GPU using BenchmarkRunner and Oracle:

- Correctness and compilation passed, numerical error 0, five measured samples
  plus one warmup for each variant; actual image/runtime/device evidence retained.
- Median latency: reference 0.003317575ms, repository 0.003610400ms.
- Dispersion: 1.573844% versus 17.992925%. Clock drift and utilization also
  exceeded the existing policy. Oracle rejected the measurements; thresholds
  were unchanged. The timing difference is not evidence of an optimization.
- Harness/reference hashes and the five measured production source hashes are
  retained. Kernel bytes were preserved. Stopped scope evidence:
  364abcc54ffb26c67ce107b4d3b5e162a24cd49ee89a8cd2c995107487aa8287.

This diagnostic ran after cancellation, separately from the full remote
optimization workflow. R8 remains OPEN for successful optimization, stable
protected whole-workflow acceptance/rollback, accepted patch delivery and actual
two-host artifact verification. All receipts and raw samples: gpu-verification.json.

## Cold-start deadline repair (2026-10-12)

Actual WSL hardware observation first returned 503 at 2.010491s, followed by
four successful warm reads. Device observation now allows five seconds while
preserving rejection on timeout; provider observation remains bounded at 2s.
The UC hardware GET allows 10s. Five subsequent actual reads passed, including
a 2.419491s cold read, with the same hardware evidence identity throughout.

The deployment gate also exposed the workspace POST's 5s timeout: its challenge
includes live identity, OCI launch and verified stop. A real HTTP regression
failed with ReadTimeout at the old limit; after raising this HTTP budget to
30s, the caller preserves both nonce files through a delayed valid response and
removes them after validation. The failed actual probe was subsequently proved
stopped. The public GPU gate passed again using the repaired client/service.
All eleven owned scopes, including both failed probes, have cgroup stop receipts.

Windows focused regressions: 45 passed, 14 platform skips in 27.10s. Two existing
dependency deprecations remain. Linux service, publication and architecture
regressions: 82 passed in 39.15s, one existing dependency deprecation. Ruff lint
and changed-code formatting passed; all seven repository guards passed.
The new Linux service regression accepts a 2.3s
identity fixture and returns 503 for a stalled 6s fixture within the 5s bound;
it never launches another GPU probe. Real socket/temporary executable tests
exercise wall-clock deadlines that async mocks cannot establish.

Portable before/after observations, gate receipt and final stop audit are in
gpu-verification.json. None establishes successful optimization acceptance.

Both independent axes found a P2 during review: HTTPX idle timeouts do not bound
continuous trickling, and an uncertain preflight POST previously lacked the
cleanup_pending lifecycle. Real socket regressions first failed, then passed
with absolute asyncio deadlines, persistent pre-submit probe context, retained
nonces and replay refusal. Identity-capable services return a matching stop
receipt; exclusive probe claims prohibit duplicate or delayed launch. Unknown
IDs can be durably fenced; active pre-create claims remain unknown. A delayed
old receipt cannot clear a newer probe owner.

An actual 0.1s client deadline against the final service caused an uncertain
contained probe. Its nonce and record remained; repeat submission was refused.
Versioned operator reconciliation obtained cgroup stop proof
737f777da68852984c026c669d325bf24a63e3925faf7c960869375cb415bb63
before removing the owned nonce directory. The final deployment gate passed with
workspace_probe_identity and a matching cgroup receipt. Full Windows Python
regressions: 1488 passed, 48 skips in 163.67s; supplemental fence cases passed.
Final Linux/focused results and independent review verdicts are recorded below.

Final Windows full suite: 1491 passed, 49 platform skips in 165.51s; two
existing dependency deprecations. Final Linux service, Ollama, publication and
architecture suite: 89 passed in 41.64s; one existing dependency deprecation.
Both independent Spec and Standards review axes passed after all fixes.

The final Standards review identified concurrent fence publication before the
creator's synchronization completed. A Linux file/directory durability regression
first reproduced the race, then passed after each positive fence observer
synchronized the claim file and directory before publishing the receipt.

The final service was restarted with this code. A real unknown probe received
workspace_fenced proof
f5ea3427db1013a2bc3ea9ad2ef18ff08c94902a430aea5667367a8257121bd4.
Its subsequent delayed POST returned 422, preserved the target and added no OCI
job records. All eleven previously audited stopped scopes remained unchanged.
The owned nonce was removed only after the matching fence proof. Portable raw
result: cold_start_deadline_repair.final_real_probe_fence in gpu-verification.json.
R8 remains OPEN; these results establish preflight/recovery safety only.

## Confirmed acceptance scope and GPU harness diagnostics (2026-10-12)

PR #710 merged at 45b848545c77deef3c24414f7da5f354981e878d with all eleven
CI checks passed. Local main matched the reviewed tree and the protocol endpoint
returned HTTP 200 with workspace_probe_identity. The user then confirmed that
no second host is available and explicitly retained incomplete cross-host
acceptance. No original criterion is waived; single-host work continues.

The failed trial's unchanged model-generated harness was replayed in its own
GPU OCI scope: all ten cases failed with JITFunction.run missing grid/warmup.
Original harness SHA: 4021f6c63349b6485092dcc30d44b4e2b6e6752258e92f709d2a38ec8dfb7f5b.
Stopped-scope proof: 250bcac3402f18f11e22902c84b7bf360ce54d056c8ed8af2217c88a06efa036.

A separately identified diagnostic copy replaced only the two kernel invocation
sites with the documented module run(x, y). Ten cases passed, numerical error 0.
An intentionally wrong run(x, y) returning x.clone() failed all ten cases.
Diagnostic copy SHA: aaf8aeb0f74d4cce486cf7ccd877124965cd3d1879076ecc708a967b4cd13f7a.
Stopped-scope proof: bf85614a3c94745996b7ef13c199b0ab9fd2a41cc56f7e12b3203853090c2f7c.
The original artifact and kernel bytes were unchanged; the copy is not genuine
model generation and no candidate was optimized or accepted.

The completed reviewer #2 executed only ls and cat and emitted its FAIL review
as an agent message. The stopped workspace contains no requested review file.
The pinned pipeline reads that file and supplies empty regeneration feedback
when it is absent. This explains a separate artifact seam; no upstream fix or
successful review is claimed. Full trial and earlier Oracle failures stand.

All thirteen owned OCI scopes were individually audited with matching container
IDs and stopped receipts. Earlier eleven-scope evidence was preserved.
Portable diagnostics and the new audit: single_host_follow_up in
gpu-verification.json. R8 and cross-host acceptance remain incomplete.

This follow-up changes eight documentation/evidence files and no production
source. All seven repository guards and staged diff/JSON scope checks passed.
Independent Spec and Standards axes passed after verifying original/copy/kernel/
log hashes, the two invocation changes, raw-to-portable report equality and
all thirteen durable stopped-scope proofs. No actionable findings remained.

PR #711 CI follow-up: the direct line-ending guard passed, but its real-repo
regression test still allowed only the earlier 2022/2013 scan count. The sole
added file since base 45b848545c77deef3c24414f7da5f354981e878d is
research/harness-loop-diagnosis.md, a text document. The measured 2023 tracked
blobs / 2014 text files therefore has an independently explained delta of one.
Before the repair, the five Windows line-ending tests reproduced one failure
and four passes. The repair adds only that reachable count pair; binary and
gitlink pins and all synthetic mutation checks remain unchanged. This follow-up
now includes eight documentation/evidence files and one CI regression test.
After the repair, all five Windows line-ending tests passed in 4.80s.
All five Linux tests also passed in 25.07s, with the shared checkout explicitly
trusted only in the test process and temporary Git sandboxes on Linux storage.
The initial WSL attempt hit Git's mounted-directory ownership check; no global
Git setting was changed. Targeted ruff passed, and both independent review axes
passed the count/inventory repair without findings.
