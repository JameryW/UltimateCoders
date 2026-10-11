# Live feedback and MetaInfer verification (2026-10-09)

## Results

The SSE transport target was measured on the actual NATS and Dashboard API
processes. Every observed sample was below 200ms, with no missing or duplicate
events across three simultaneous HTTP SSE clients in either run.

| Traffic | Events per client | Clients | Highest client p99 | Maximum sample |
|---------|-------------------|---------|--------------------|----------------|
| Steady, 20ms publish interval | 300 | 3 | 2.260ms | 6.394ms |
| Burst, no publish interval | 300 | 3 | 55.270ms | 55.712ms |

Conditions: Windows 11 build 26300, Python 3.14.3, NATS Server 2.14.6,
nats-py 2.15.0, httpx 0.28.1, FastAPI 0.141.1, Uvicorn 0.52.1 and
sse-starlette 3.4.8. Broker, API and measuring clients ran on localhost.
The Dashboard implementation was from UC commit
9c70e329f65eef5f1fcda02932abb0efa6836ae6.

The measured boundary starts immediately before publishing to NATS and ends
when the measuring client receives a complete HTTP SSE frame. Publisher and
consumers share a monotonic clock. Worker execution, Gateway processing,
frontend rendering, Nginx proxies and cross-host latency are outside this
measurement. This is a localhost transport result, not a deployment-wide SLA.

All 1,800 sequence-indexed samples and the pinned deployment checks are saved
in [the raw evidence](../.trellis/tasks/10-02-metainfer-architecture/live-verification.json).

## Reproduce the SSE measurement

Use an isolated broker and Dashboard API: the script publishes synthetic
uc.task.event messages, which enter the Dashboard event log and metrics.
Install the ordinary Dashboard dependencies and NATS Server 2.14.6.
Start the broker in one terminal:

```powershell
nats-server -a 127.0.0.1 -p 44222
```

Start the source Dashboard in another terminal, from the repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path python).Path
.venv/Scripts/python.exe -m ultimate_coders.dashboard --host 127.0.0.1 --port 48080 --nats-url nats://127.0.0.1:44222
```

Run both traffic patterns:

```powershell
.venv/Scripts/python.exe scripts/verify-sse-latency.py --dashboard-url http://127.0.0.1:48080 --nats-url nats://127.0.0.1:44222 --output .uc/sse-steady.json
.venv/Scripts/python.exe scripts/verify-sse-latency.py --dashboard-url http://127.0.0.1:48080 --nats-url nats://127.0.0.1:44222 --interval-ms 0 --output .uc/sse-burst.json
```

On Linux/WSL, use PYTHONPATH=python and .venv/bin/python instead. The script
warms up until all clients receive a probe through NATS, then measures 300
events per client. It observes delayed duplicates for 250ms after completion.
Success requires complete delivery, zero observed duplicates and **every**
sample strictly below the threshold. A missing event or slow outlier cannot
be hidden by a low percentile. JSON retains each sample and missing sequence.

Exit codes: 0 passes; 1 records a failed measurement; 2 rejects arguments;
3 reports an unavailable service or unsuccessful warmup without producing a
measurement. The timeout bounds initial connections and completion waits;
the configured publish interval also contributes to total run time. Cancelling
HTTP consumers and closing the NATS publisher also have explicit deadlines.
Optional authentication reads DASHBOARD_PASSWORD, or the environment variable
named by --token-env, without putting the token in command arguments.

Negative checks were executed against the real API: a 0.0001ms threshold
returned 1 with passed=false; an unreachable API returned 3 without creating
an evidence file. Fourteen focused regression cases cover missing events,
duplicates, individual slow samples, invalid numeric configuration, fragmented
multiline frames, transport cleanup and a stalled NATS close.

## Pinned MetaInfer deployment

The unmodified upstream was checked out at
b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890 and started in Ubuntu 24.04 under
WSL with Python 3.12.3, using an isolated virtual environment and its official
requirements. No upstream source is vendored into UC.

The local service listens at http://127.0.0.1:48765. The HTML page and
GET /api/sys-shell/task-types returned 200. GET /api/uc/contract and
GET /api/uc/hardware returned 404. The actual release gate returned 2:

```text
REJECTED: Mutating MetaInfer jobs require the UC workspace/stop contract
```

No release-pass evidence was generated. WSL also observed the real RTX 4060
Laptop GPU, UUID GPU-0e9353b0-7678-a5ab-eb56-083ecede327f, 8188MiB and driver
617.14. Device visibility does not establish MetaInfer GPU execution.

To reproduce the upstream startup in a WSL terminal at the repository root:

```bash
git clone --no-checkout https://github.com/HuangPuStar/MetaInfer.git .uc/metainfer-pinned
git -C .uc/metainfer-pinned checkout --detach b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890
python3 -m venv .uc/metainfer-venv
.uc/metainfer-venv/bin/python -m pip install -r .uc/metainfer-pinned/requirements.txt
cd .uc/metainfer-pinned
METAINFER_ROOT="$PWD/../metainfer-home" METAINFER_NODE_ID=uc-stock ../metainfer-venv/bin/python serve.py --host 127.0.0.1 --port 48765
```

R8 remains open. A separately deployed extension must implement all four
endpoints in [the UC service contract](metainfer-service-contract.md), with
real execution containment or workspace fencing behind the quiescence proof.
The current launcher sends process-group signals; a signal acknowledgement
does not prove all descendant writers stopped. The acceptance run must then
exercise optimization, protected benchmarks, Oracle accept/reject, rollback,
commit/artifact delivery, cancellation and restart recovery. No such execution
is claimed by this deployment probe, and the contract gate remains enforced.

## Contained service deployment (2026-10-11)

The separate UC extension reuses the pinned shell/orchestrator without calling
upstream's native app startup or janitors. Verification used rootful Podman
4.9.3 inside Ubuntu 24.04 WSL, Python 3.12.3 and local cgroup v1 evidence. The
immutable CPU job image was
2ec839ab5cceab141c49a424b1a67d50d43b725717780be5d036ec1e0781b068.
Deployment commands and supported boundaries are in
[the service guide](metainfer-contained-service.md).

Measured results:

| Check | Result | Evidence scope |
| --- | --- | --- |
| Detached writer recovery | Passed | A fresh launcher process observed the original container and continuing child writes; cancellation stopped writes before a durable cgroup receipt |
| Cross-UID workspace challenge | Passed | UC probe UID 65534 and OCI job UID 1000 exchanged the same random nonce through the assigned workspace |
| Pinned HTTP task creation/control | Passed | Create 200, restart denied 409, kill 200, identity-checked stop proof with writers_stopped=true |
| Unsafe task metadata | Passed | Host-file symlink returned 503 without leaking content; omitted arbitrary agent-log route returned 404 |
| Failed submission visibility | Passed | Rejected launch remained observable and task listing returned 200 |
| GPU/model release gate | Open | Hardware endpoint returns 503; no GPU/model attestation or accepted optimization evidence |

The writer recovery command is scripts/verify-metainfer-containment.py from
the service guide. It starts two separate launcher processes; this proves
durable launcher recovery rather than an active-job restart of the HTTP daemon.
The public scripts/verify-metainfer-release.py workspace gate was also run from
a distinct UID against the actual HTTP service. The HTTP control probe used
a disposable linked worktree and the pinned evolve-kernel task form, then
cancelled it; this was a lifecycle check, not a successful kernel optimization.

Sanitized receipts, exact identities and HTTP outcomes are preserved in
[service-verification.json](../.trellis/tasks/10-02-metainfer-architecture/service-verification.json).
The local raw runs remain under the ignored .uc verification directories.
Faults found during these runs included job-directory ownership, short-process
inspection races, kill racing with natural exit, unsafe host metadata reads and
upstream native startup escaping the container scope. Their fixes have direct
regressions. Final Linux service regressions: 25 passed; one upstream dependency
deprecation warning.

R8 remains OPEN. GPU device visibility inside a contained job and an available
local Ollama server are provisioning evidence only. The hardware attestation
path and actual optimization, protected benchmark/Oracle accept and reject,
rollback, commit/artifact delivery and active-run cancellation/recovery still
need real acceptance evidence. Neither the CPU receipts nor this report claim
that release criterion has passed.

## Contained hardware and native coding (2026-10-12)

The explicit native Ollama profile now supplies actual hardware attestation.
The final image f2d47104f71e50bd62d8c9910c5e351d3a7ef1db63c2e489f3316f7bdd20d411
executed three Torch/Triton vector-add cases (33, 4096, 65536 elements), all with
maximum error 0, and a random model nonce request. Observed runtime: Torch
2.6.0+cu124, CUDA 12.4, Triton 3.2.0; the RTX 4060 Laptop UUID and driver match
the earlier inventory. Ollama model uc-metainfer-gemma:1012 was pinned to digest
0979dc60650788ee9dc535e6d808fa5ac9b7790f33f38d0a207bab93caa7291f.

A fresh launcher reused cached hardware evidence while a writer's file kept
changing, then stopped that writer with a cgroup receipt. The production native
CLI made two genuine tool calls to create/test code; an independent second
container verified positive, negative and fractional inputs. The actual HTTP
service passed scripts/verify-metainfer-release.py --require-gpu on port 48768.
Profile instructions are in [the deployment guide](metainfer-contained-service.md).

This establishes hardware/model and coding preflight, not accepted optimization.
R8 remains OPEN. The older streaming attempt repeatedly failed harness review
and was stopped; independent Git comparison confirmed unchanged repository bytes.
Complete optimization, protected Oracle outcomes, rollback, delivery and actual
two-host artifact verification still need passing evidence. See
[portable GPU receipts](../.trellis/tasks/10-02-metainfer-architecture/gpu-verification.json).
