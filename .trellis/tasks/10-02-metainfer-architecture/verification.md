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

## Native artifact continuation (2026-10-12)

Baseline: c12f2e49998f9e76f567aa5c4b50e0172a470fac, merged PR #711 with eleven
passing checks. Branch: codex/metainfer-ollama-context. This delivery repairs
the native Ollama profile/session/file boundaries and Windows checkpoint
publication found during the still-open R8 single-host work.

The actual context and output limits now reach every chat and bind launch,
cache and sessions. Both implicit-default override paths were reproduced and
fixed before workspace claim/create. write_file publishes actual structured
provider bytes through no-follow FDs; it does not synthesize a requested file
from model prose. File and message item IDs are distinct. Two independent
review axes found output exhaustion still accepted at done=true; real HTTP
client/CLI text and complete file-call tests first failed and then passed.
The final stream boundary rejects done_reason=length before tools/session
publication/success. A cleanup-error regression also demonstrated an open
directory FD; nested finally now closes it while propagating the error.

An actual Windows reader without FILE_SHARE_DELETE reproduced WinError 5.
Transient release now permits atomic replacement; a persistent reader exhausts
five attempts, preserves original bytes, removes only the owned temporary and
raises. Backoff is 10/20/40/80ms; other platform/error paths are immediate.

Final checks on the delivered source:

- Windows complete Python suite: 1509 passed, 61 platform skips, 264.45s.
  Two existing dependency deprecations and one local cache-directory warning.
- Linux service/Ollama/publication/architecture/runtime-state: 118 passed,
  3 skips, 88.46s; one existing dependency deprecation. The initial NTFS temp
  directory failed FIFO/mode/Git fixture semantics; rerunning on native /tmp
  passed without weakening tests or adding skips. CLI sessions are isolated.
- Six focused output-exhaustion/cleanup/normal-file checks: passed, 9.07s,
  after the respective RED reproductions. Two actual Windows-handle cases
  passed after their RED reproductions.
- Ruff across python/ and tests/ passed. All seven repository guards passed:
  spec refs, task refs, line endings, workflow inputs, Codex issue flow,
  README CI table and journal ledger. Existing advisory inventories stand.
  Tracked census remains 2023 blobs / 2014 text / 9 binary / 1 gitlink;
  no files or guard allowlists were added for this delivery.

Immutable image 12098cc317e4ae7f07d15a96fb98509b40b1d668db6ce7da0392b9a1db93d06a
matches the final normalized native code hashes. Actual Torch 2.6.0+cu124 /
CUDA 12.4 / Triton 3.2.0 vector-add cases 33, 4096 and 65536 have zero error.
The installed Ollama 0.40.3 native GGUF profile uses a separate 9B alias pinned
to digest 7bb044cb04582112c39bb1474b9c6c087cee3a3bfbd4167e335a84e37e8516a5,
context 16384 and output 8192. Existing aliases/weights were preserved; no new
model download or nonce-text repair was used.

The final model actually wrote the exact random marker through write_file;
its file_change, final event, byte checksum and stopped scope were observed.
The service reused the stopped hardware cache and passed the real HTTP GPU
and shared-workspace gate. The first operator invocation mistakenly supplied
the coding alias to --model, which filters GPU device names; it correctly
rejected. The corrected RTX 4060 filter passed. Both workspace probe scopes
are in the final audit. All 27 owned scopes on boot
9e4d7211-2d7f-4c91-8965-fd35047454ba were individually observed stopped;
thirteen earlier-boot records are retained as historical, not revalidated.

Genuine harness generation is still unsuccessful as acceptance: one unchanged
harness failed JIT detection, another used torch.randn for an excluded integer
dtype, and the final review hit the real 180s header deadline with no successful
event/file. Earlier context/output/template failures are retained. Generation
completion and container-lifecycle completion are not correctness success.
Raw reports/log hashes, reference and artifact hashes, exact file result,
HTTP hardware/workspace receipts and each stop receipt are appended as
native_artifact_continuation in gpu-verification.json. All reference bytes
remain 87c37016af10e1959103a34bcbcec75e306f2975848f1b5f33282807eb798e67.

Source Spec and Standards review axes both passed after the exhaustion and FD
fixes; final evidence review follows. This is preflight and artifact-seam
validation. Successful full optimization, protected whole-workflow Oracle,
rollback, accepted candidate delivery and explicitly deferred two-host checks
remain OPEN. The task is neither completed nor archived.

Final independent Standards/Evidence review: PASS, no actionable findings.
It matched eight raw reports, eleven logs, four model files, twelve reference
files and all 27 record/receipt groups, then observed all 27 containers stopped
with PID zero and no remaining cgroup writers. Final Spec/Evidence review:
PASS, no actionable findings. It independently matched those raw/portable
hashes, final marker/events, five image code hashes, thirteen historical
records, four actual model metadata observations and two fixed server log
snapshots with sixteen exact line-numbered entries. The source exhaustion P2
remains repaired. Full acceptance criteria and open task status stand.

The reviewed continuation is tracked by PR #712. Its CI and merge status are
the delivery record; the task metadata now links this PR while retaining the
merged c12f2e49 continuation baseline. This association changes no source or
acceptance outcome.

## Harness acceptance continuation (2026-10-12, baseline 61a65556)

Branch codex/metainfer-harness-acceptance repairs three observed integration
boundaries and records the production UC workflow's actual failed outcome.
No upstream source, model weights, original kernel or Oracle threshold changed.

The native opener silently capped response headers at 180s despite a longer
caller budget. An actual local HTTP peer delayed headers 183s under a 190s
budget: the old code failed at 180.474523s, the repair succeeded at 183.704007s.
The subprocess still enforces the whole operation deadline, including DNS,
headers and continuously trickling bodies. The long peer is transport evidence
only. Real-connection budget/delayed-header regressions first failed then passed.

Nine real model checkpoints remain distinct from acceptance. Fresh Qwen failed
embedded Triton source inspection and then a four-argument call to run(x,y).
An actual repaired harness passed 27 reference cases while bypassing that public
wrapper. Its bit-identical diagnostic copy also passed 27 compatible wrong-run
cases, an observed false acceptance. The original negative missing the raw
kernel was a malformed ABI fixture, not a numerical rejection. A later genuine
repair passed 46 reference cases and rejected the compatible wrong wrapper
(2/46 passed); its FAIL review correctly found missing maximum length, while
excluded-input demands do not extend the contract. Gemma passed 11 reference
cases and rejected all 11 wrong-wrapper cases, then received a genuine FAIL
review for relevant boundary/guard coverage. Its next artifact contained a
Tensor JSON field and reached the 720s checkpoint deadline. A Qwen repair
exhausted 16 turns; precise observed-line feedback finally caused a real
minimal bool edit, but unchanged uninitialized poisoned inputs still rejected
the reference. No operator-corrected artifact is counted as generation success.

The first production workflow failed before GPU measurement because the local
controller's relative PYTHONPATH was evaluated in the assigned worktree. After
fixing that ignored controller, the next workflow completed protected baseline
measurement but submitted max_iterations="1", outside the pinned form's string
choices "10"/"20"/"50". HTTP 400 left submission_unknown, quarantine and capacity
retained; no remote task exists in the observed service registry. This isolated
verification state has not been cleared or replayed. The client repair checks
live select/multiselect labels before reservation, intent and POST. Five invalid
and four legal regression cases reproduced 5 failures/4 passes, then all passed;
both platforms passed all 25 adapter tests. The actual HTTP service now refuses
that invalid value locally with unchanged task list and no occupied capacity.

An independent production run used an allowed "10" inner iteration choice and
one outer UC iteration. Its six protected baseline invocations produced five
measured latency samples around 0.003258ms with 0.1262% dispersion and zero
numerical error. Benchmark code and input repository were mounted read-only in
separate owned OCI scopes. The unchanged baseline, protected scripts/settings,
hardware conditions, measurements and individual stop receipts are recorded.
The task reached performance review, then the 900s remote execution deadline.
UC obtained quiescence, entered rollback, recorded failure and released that
run's GPU/backend reservations. Repository bytes and Git baseline remain
unchanged. Candidate Oracle evaluation and accepted patch/commit delivery were
not reached; this is a real failure/stop/rollback path, not successful R8.

The production run also exposed a pin-specific parsing error: the unchanged
review file explicitly says "## Verdict: FAIL", yet the fallback count of PASS
substrings (including passed) versus FAIL words returned PASS. A state transition
named "harness approved" therefore is not valid approval evidence. The native
client now verifies any existing correctness/performance review before final
message/turn completion, requiring one leading supported bold verdict, with
bounded regular-file no-follow reads. It preserves bytes and refuses ambiguous
formats from either a real file tool or shell. A valid FAIL is still delivered
as FAIL. Two invalid CLI cases first failed and four valid PASS/FAIL cases passed;
all six now pass. Ten format and five real FD/link/FIFO/size cases also pass.
Both original failed reviews were replayed unchanged in the final image: the
new guard rejected them, including the one the actual pinned parser calls PASS.
This is a protocol guard, not semantic harness or Oracle validation.

Three preexisting tests assumed sub-tick time advance on Windows Python3.9.
Deterministic actual clock sources now verify 10 events over 1s = 600/min and
task transition timestamp refresh/no-op stability. Production timing logic is
unchanged. Windows3.9 adapter/clock tests: 80 passed, 3.91s; Windows3.14 clock:
55 passed, 3.23s; Linux clock: 55 passed, 5.90s. An initial Windows3.9 full run
had 1494 passes, 61 skips, three repaired timing failures and 25 setup errors
because no cp39 Rust extension is installed. It is not a full-suite pass. Other
initial interpreter attempts lacked pytest or service dependencies and were
environment setup failures; no tests or acceptance criteria were skipped to
hide them. The final ABI-matched Windows3.14 full suite passed 1532 tests with
72 platform skips in 197.70s. The final Linux related suite passed 223 tests
with three skips in 83.84s; one existing dependency deprecation. Ruff passed.

Earlier image6 b2456544 remains immutable with its frozen two-file build sources
and original failed workflows. Final image7 233de64c35a1bf17d35bc7947275abe8fa8d382f8604fe64b5f1bcfab3b3c0ae
binds all five current native code hashes and fresh observed GPU/model evidence.
Actual native write_file delivered the exact random marker and completion event;
CUDA/Triton cases 33/4096/65536 and the model nonce passed. A separate new control
profile replaced this task's owned old v5 HTTP daemon on port 48768 after exact
process/boot/backend verification; its old deployment pointer was preserved.
The final HTTP GPU/shared-workspace gate passed. Native WSL Podman plus Windows
Ollama remains the local validation setup; this does not certify a whole-app
Podman deployment or the complete OS/GPU/model matrix.

All raw reports, model file hashes, native event logs, protected workflow
artifacts, benchmark stop receipts and final deployment/audit evidence are in
harness_acceptance_continuation of gpu-verification.json. Historical evidence
is retained; full successful optimization, protected candidate Oracle outcomes,
accepted delivery and explicitly deferred two-host acceptance remain OPEN.

Root-cause prevention: (1) timeout ownership was split between caller and hidden
transport ceiling, fixed by one remaining budget with real boundary tests;
(2) the client reproduced only part of the live form protocol, fixed by option
validation before irreversible intent; (3) the upstream heuristic confused prose
with an authoritative verdict, fixed by a native delivery-format boundary;
(4) positive self-reference tests hid API bypass, exposed by a compatible numeric
negative; (5) host clock precision made timing assertions nondeterministic,
fixed by controlling actual clock inputs without weakening production behavior.

Final observed audit: 74 owned OCI scopes on the current WSL boot were
individually verified stopped, with matching immutable image/container/cgroup
identities and hashed receipts. Thirteen older-boot records remain historical.
The prior invalid submission's isolated quarantine/reservations remain retained;
stopped indexed containers do not retroactively resolve an unknown operation.

Final independent Spec/Evidence review: PASS, no actionable findings. It matched
the nine checkpoints, three workflows, native/model file hashes, frozen image6
and current image7 sources, and separate old27/70/final74 audits. Read-only SQLite
confirmed b247 stopped/released and 9a still uncertain/occupied; HEAD and repository
bytes match the initial snapshot, and baseline statistics were independently
recomputed. The pinned source remains clean at b3f6505a.
Final independent Standards/Evidence review: PASS, no actionable findings. It
matched 348 file hashes, 18 raw JSON groups and 192 proof validations, and
observed all 74 current-boot containers stopped with PID zero and no cgroup
writers. R8 and cross-host acceptance remain OPEN.
Ruff, git diff --check and all seven final repository guards passed. Tracked
census remains 2023 blobs / 2014 text / 9 binary / 1 gitlink. No new tracked
files, census exceptions or guard allowlists were added.

## Submission identity continuation (2026-10-13)

Baseline PR #713 / 02cfc0b4 is merged and the pinned source remains unchanged.
The current daemon reproduced HTTP 404 for `/api/uc/jobs/<known-task>/identity`,
although UC's existing operator attach command calls that route. The extension
now advertises `job_identity`, binds the full UC operation label and canonical
task request SHA privately before OCI create, and observes the matching immutable
container before returning identity. Job-writable metadata is not trusted;
legacy/missing/foreign jobs refuse without releasing ownership or reservations.

Final source review: Spec PASS and Standards PASS, no actionable findings.
Windows full suite: 1543 passed, 74 platform skips, 288.76s. Additional final
identity assertions: 53 passed, 33 skips, 24.08s. Linux related suite: 181 passed,
1 skip, 94.45s, with the existing FastAPI/Starlette deprecation. Ruff passes.

Real OCI/HTTP controlled identity recovery passes. A live container rewrote its
requirements label to `UC foreign`; the private binding still matched the actual
operation/request/container. Production versioned attach retained the active
writer, workspace and capacity. Separate stop confirmation obtained a cgroup
receipt, stopped marker growth and released capacity. The temporary daemon also
stopped. This is a controlled direct-launch fixture, not pinned submission loss
or model optimization. Preserve the first operator-script quoting failure and
its actual stopped scope; its UC state was recovered through matching identity,
attach and independent stop confirmation rather than manual record clearing.
Portable originals/checksums are in `job_identity_continuation` of
gpu-verification.json; previous manifests and audits are unchanged.

A fresh genuine image7 workflow, wholeflow-026410c9d4, used a 3600s execution
budget and the valid inner iteration choice `10`. Six protected GPU
baseline calls completed: median latency 0.0032604001462459564ms, dispersion
0.20867316863282018%, zero numerical error. The first generated harness failed
28/28 cases with undefined run_ref; actual model tools repaired it to 28/28.
Reviews then failed with Ollama HTTP 500; regenerated code also had Tensor
boolean errors. A genuinely written 18754-byte review used a heading first and
was refused by the native verdict guard before completion. The same file then
incorrectly blocked generation call #5 although only the harness changed. Its
actual file_change -> validate_review error reproduces the stage-boundary bug.
The operator stopped this identified blocked task before budget expiry, obtained
a matching cgroup receipt, and UC observed failure, rolled back unchanged kernel
SHA 87c37016...8e67 and released both reservations. This was operator stop, not
an execution deadline success or candidate Oracle acceptance.

The repair snapshots bounded no-follow regular review bytes only for the exact
original pinned correctness/performance generator headers. Only unchanged entry
bytes are preserved; every new/changed review and reviewer/generic turn remains
strict. RED: 4 failed/16 passed in 50.28s, through actual HTTP and file/shell tools.
GREEN review checks: 41 passed in 63.27s. Final Windows full: 1544 passed, 94
platform skips in 208.03s. Final Linux related: 201 passed, 1 skip in 126.02s.
Ruff passes. These final results supersede the earlier identity-only runs above.

Immutable image8 877e2b53 passes actual GPU/model attestation and two genuine
native file calls with the original rejected review copied unchanged. The
generator call completes; the generic call writes its marker then refuses the
same review before completion. Original review bytes are preserved in both;
this is native boundary verification, not genuine harness acceptance. The old
v7 daemon was retired through verified PID/boot/image/root identity and its
deployment snapshot retained. Image8 is deployed at 127.0.0.1:48768 with the new
identity capability and a fresh public GPU/workspace gate. A new audit observes
all 88 owned current-boot scopes stopped, PID zero, empty/missing cgroups and
released private workspace owners. Thirteen older-boot records remain historical.
Previous 27/70/74-scope audits and frozen image6/image7 code remain unchanged.

R8 remains OPEN. The earlier invalid submission remains quarantined in its
isolated verification DB; the new binding does not retroactively resolve it.
Actual cross-host acceptance remains deferred by the user for lack of a second
host. These identity fixtures do not satisfy that criterion.

Final combined Spec/Evidence review: PASS, no actionable findings. It matched
the private identity and image8 boundary outcomes, raw file/proof hashes,
88 stopped observations, empty ownership stores, frozen sources and the
unchanged v7 rollback baseline. Final Standards/Evidence review: PASS, no
actionable findings; raw reports, all-writer observations, reservations and
current HTTP identity match. The final read-only contract/hardware/existing-job
identity GET snapshot contains three HTTP 200 responses and creates no probe;
the main session obtained it and independent reviews checked the saved bytes.
Ruff, git diff --check and all seven repository guards pass; tracked census
remains 2023 blobs / 2014 text / 9 binary / 1 gitlink, without new tracked files
or guard exemptions. CI/merge will be recorded by the PR; R8 remains OPEN.

PR #714 merged on 2026-10-13 at 12:51 UTC as
1e7dc7fa82b0540444ac0a1cc91b40eaf936958c. All eleven checks passed, including
the complete Python 3.9/3.12 CI suites, Dashboard and repository guards. Local
main was synchronized before the new Gemma verification. This source delivery
does not establish successful optimization, candidate Oracle or accepted delivery.

## Genuine Gemma workflows (2026-10-13)

Both trials used immutable image8 877e2b53, the unmodified pinned MetaInfer
b3f6505a, installed Gemma alias uc-metainfer-gemma:1012 with digest 0979dc60,
16384 context tokens, separate service roots and a 3600s controller budget.
The explicit profiles differed in output budget, 4096 versus 8192. Their
deployment snapshots inherited historical pending-source and start_script
metadata; actual source was the merged PR #714 baseline above. Each portable
trial explicitly identifies its actual Gemma start script. The inherited script
is not a reproduction command for these profiles. No automatic provider fallback,
model download, upstream fork or Oracle threshold change was introduced.

The 4096 trial wholeflow-c72beb6993 completed six protected baseline calls:
median latency 0.0032600000500679015ms, dispersion 0.2471224780302444%, zero
numerical error. Actual model tools wrote two harness versions and a 2014-byte
leading `**Verdict**: FAIL` review; three native turns completed, with no
command-execution events. The review contains genuine coverage concerns and
an incorrect portability complaint about the supplied absolute reference path;
it remains original model output, not an operator-endorsed assessment.
Regeneration first failed invalid file arguments and then wrote a new harness.
The next review exhausted 4096 output tokens twice. Operator stop settled the
task; it did not time out its 3600s controller budget or reach a candidate Oracle.

The 8192 trial wholeflow-a55268040f completed six protected baseline calls:
median latency 0.003273100033402443ms, dispersion 0.27035715892643736%, zero
numerical error. The first generator call omitted the required `path` argument:
its original session was copied read-only while the container was still alive.
Only `content` was present, and the client refused the call without inferring a
filename. Retry wrote a 4080-byte harness and completed one native turn, again
with no command-execution event. Two review attempts exceeded the existing
600s chat limit without writing a review. The operator then stopped the third
attempt and preserved its started events; this was not a successful review or
an exhausted 3600s controller deadline. No model artifacts were rewritten.

Both independent settlement checks matched actual worktree HEADs to their
starting commits and kernel SHA 87c37016...8e67. UC states are failed, remote
records stopped, GPU reservation lists empty and both capacity budgets idle
with no holders or waiters. Each benchmark report has an empty iterations
array; its initial optimized field mirrors the baseline and is not a candidate
measurement. Accepted patch/commit delivery and protected candidate Oracle
acceptance/rejection remain unverified.

A separate audit observed all 106 current-boot owned OCI scopes stopped,
actual PID zero and matching empty/missing cgroups. Thirteen earlier-boot
records remain historical. Earlier audits and portable sections are preserved.
The failed 4096 session's tmpfs was already gone after stop, so its diagnostic
copy was unavailable; only the 8192 original is recorded as a copied session.
The first diagnostic helper's filename lookup failed before any copy; its
original script and the subsequent unavailable-tmpfs attempt are retained.

The operator's Ollama unload request returned `done_reason: unload` while
the selected model was still loaded. Provider disappearance is observed
separately; the acknowledgement alone is not a cleanup pass and is never used
as a remote writer-stop proof. R8 stays OPEN, as does the user-deferred actual
two-host acceptance. The earlier unknown submission remains quarantined in
its isolated verification DB; observed stopped scopes cannot resolve it.

The seventeenth provider observation confirmed that the selected Gemma model
was absent. Preserve the earlier unload-acknowledged-but-loaded report and the
recent provider log window separately. Final cached contract/hardware/known-job
identity GETs returned nine HTTP200 responses across all three services,
without adding a job or probe. Read-only SQLite inspection independently
confirmed the previous cleanup_pending/submission_unknown quarantine and its
holders remain intact. Seventy-five prior identity-continuation file descriptors
were checked unchanged before sealing the append-only Gemma evidence section.

Final Spec/Evidence review: PASS, no remaining actionable findings. It matched
94 newly referenced files, 29 raw reports, 132 stop-proof references, native
events, protected samples and actual Git/SQLite settlement. Final
Standards/Evidence review: PASS, no hard standard breach or actionable Fowler
smell. It independently matched 106 control records/receipts, 21 ownership
databases, actual cgroups, preserved history and both settlement states.
The review's snapshot-source ambiguity was resolved by actual_start_script
descriptors and historical-field explanations without changing original bytes.
All seven repository guards and diff check pass. Implementation code is
unchanged in this continuation; its full test/CI delivery is PR #714 above.
