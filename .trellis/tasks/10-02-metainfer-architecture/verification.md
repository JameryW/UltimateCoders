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
