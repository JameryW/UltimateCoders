# MetaInfer reliability verification

Date: 2026-10-02. Source baseline: `7d6bfbfa`. This repair addresses the execution,
recovery, delivery and evidence issues identified after the MetaInfer integration.

## Verified behaviors

- A planner-created inference task with empty file constraints runs in a real Git
  worktree. A fixture HTTP service changes latency evidence from 43 to 37; repeated
  protected benchmarks accept it, and UC commits/merges the edit into a clean base.
  Allocation failure never executes shared code. HEAD fallback retains its real path.
- Lost submission response creates one remote job intent and prevents another POST.
  Restart attaches a known remote ID. An unconfirmed writer retains candidate code,
  rollback transaction and the original baseline. Completed-job recovery releases
  a concurrency slot left behind by a crash.
- Concurrent duplicate dispatch starts one execution. An immutable persisted outcome
  survives disconnected publication and restart. Delivery recovery after worktree
  removal returns the previous merged commit without running the adapter again.
- Coordinator restart recovers current Gateway attempts and confirms only after
  complete-snapshot persistence. Both old and future confirmed attempts are fenced.
  Same-attempt full/partial replay cannot revive cancelled nodes; paused completion
  remains pending until resume. Duplicate node registration retains every cancel handle.
- Delayed runner startup and worktree takeover share a lease transaction and validate
  process birth. Gateway hydration supports UTC Z/nanoseconds on Python 3.9 and retains
  verification commands, project scope, user constraints and capabilities.
- Statistical acceptance rejects unstable samples, missing environment/sample
  metadata, and improvements within measured noise. Every repetition contributes to
  hard peak-memory/error ceilings; missing constrained evidence cannot be hidden.
- Artifact reads enforce authentication, root containment, download bounds and
  SHA-256 integrity. Internal transaction checkpoints are not downloadable.

## Validation commands

```powershell
.venv\Scripts\python.exe -m pytest tests/python -q --no-cov -p no:cacheprovider
.venv\Scripts\python.exe -m ruff check python/ultimate_coders tests/python
cargo test -p uc-grpc --features messaging,storage --lib
cargo test -p uc-engine
```

Dashboard validation: `npm run lint`, `npm run build`, `npm test` (12 tests passed).
Fault regression: `test_inference_delivery.py` and `test_inference_reliability.py`
(20 tests passed; combined with the cancellation suite: 31 passed, 1 skipped).
Gateway library: 257 passed. Engine: 456 unit and 5 integration-style tests passed.

## Live deployment checks

The local app uses the native WSL Docker engine. BuildKit cannot read some Windows
cache ACLs in the checkout, so builds use a source-only archive extracted into a
dedicated Linux temporary directory. No storage volume or application repository
is replaced by that build context. PostgreSQL integration tests create and remove
their own private database. NATS/Gateway confirmation tests use isolated subjects
and a private broker to avoid purging the application's task stream.

PostgreSQL: 2 integration tests passed, including concurrent cold starts/mutations.
Private live NATS/Gateway/PostgreSQL: 1 integration test passed, verifying terminal
outbox confirmation, persisted task/node state and coordinator restart replay.
The fixture uses the Gateway's initial attempt 0; it does not fabricate a dispatch counter.
Private broker, Gateway container and test database were removed after verification.

Final Python suite: **1365 passed, 11 skipped**, with two dependency deprecation
warnings. Ruff, line-ending, task-reference, specification-reference and issue-flow
guards passed. The task reference census is 853 valid references, with no dangling
or malformed entries; the specification audit has no unclassified references.

All ten application containers are running; Gateway, PostgreSQL, NATS, TiKV,
Qdrant and PD report healthy. Gateway, Worker, coordinator and Dashboard API use
the same runtime database. Experiment list API returned 200 and read the shared
store; absent MetaInfer advertises only the local inference benchmark capability.
The final Worker reached the configured Ollama model and passed a real fixed-output
inference Oracle. Windows Ollama and its existing WSL forwarding helper were
restarted after they had stopped. The local app remains at
<http://127.0.0.1:8081/dashboard>.

Two independent final reviews have no remaining evidenced Standards findings or
reproducible Spec P1/P2 findings; see the task review artifact.

## Boundaries

CPU fixture latency numbers verify acceptance and delivery mechanics, not GPU
performance. External MetaInfer/GPU optimization is still unconfigured locally.
The pinned stock service was inspected through its public source, but its `kill`
response, `finished` flag and PID status do not satisfy UC's all-writer
quiescence contract; therefore no real MetaInfer acceptance, cancellation or
GPU result is claimed.
The previous [Ollama deployment verification](local-deployment-verification.md)
remains separate evidence for real local-model coding/benchmark execution.
Native WSL Docker requires a running WSL session; Ollama and its forwarding helper
must remain active. Docker Desktop's earlier host socket limitation is unchanged.
Cross-host artifact sharing and operator reconciliation of uncertain remote writers
remain deployment responsibilities. A memory-only Gateway intentionally cannot
certify durable delivery. Failed Gateway event writes retain the outbox until
storage is fixed and the Gateway restarts.

## Real GPU identity evidence (2026-10-04)

R7's acceptance requires *actual* device identity, not a declared one. The host
this runs on does carry a real NVIDIA device, so this section records what
`ultimate_coders.inference.hardware.capture_environment` observed through
`nvidia-smi`, unmocked:

| Field | Observed |
|-------|----------|
| UUID | `GPU-0e9353b0-7678-a5ab-eb56-083ecede327f` |
| Model | NVIDIA GeForce RTX 4060 Laptop GPU |
| Driver | 617.14 |
| Memory | 8188 MiB |
| CUDA compiler | nvcc 12.6 (`V12.6.85`, `cuda_12.6.r12.6`) |
| Temperature / SM clock / utilization | 54 C / 210 MHz / 0 % |

Conditions were sampled while the device was idle, which is the correct baseline
for a measurement run. `dependencies` is empty: no `torch`, `triton`, `sglang`
or `vllm` is installed in the measurement environment, so no ML-stack version is
part of this fingerprint and none is claimed.

Two acceptance points were then exercised **against this device**, not against
a fixture:

- A declaration matching the observed UUID, model and driver is accepted and the
  device is recorded.
- A mismatched `gpu_uuid`, `gpu_model` or `gpu_driver` is each rejected with
  `Declared <field> differs from actual hardware`.
- A workload declaring `require_gpu=true` with no observable device
  (`CUDA_VISIBLE_DEVICES` empty) is rejected with `Declared GPU workload has no
  observable GPU`.

This closes R7's "declared hardware must match actual hardware" and "a missing
observable GPU must fail" acceptance points with real hardware evidence. It does
**not** close R8.

### Still absent: the pinned MetaInfer service

`nvidia-smi` is present; `metainfer` is not. No MetaInfer process, package or
checkout was found on the host. `scripts/verify-metainfer-release.py` therefore
could not be run and no service-side evidence exists.

That script stops before creating a remote job by design, and it requires a
service exposing UC's extension endpoints -- `/api/uc/contract`,
`/api/uc/workspaces/verify` and `/api/uc/hardware`. Stock upstream MetaInfer does
not carry those, so pointing the gate at an unmodified upstream would correctly
fail the contract check rather than produce evidence. The remaining input is a
MetaInfer deployment that carries the UC service extension at pinned revision
`b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890`.

Until that exists, no MetaInfer acceptance, cancellation, recovery or artifact
delivery result is claimed, and the CPU fixtures are not a substitute for one.
