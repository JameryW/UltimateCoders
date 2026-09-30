# Local deployment verification (2026-09-30)

The production Docker app was built and run locally with a real Ollama model.
The running Dashboard is <http://127.0.0.1:8081/dashboard>. This report records
observed behavior; optional integrations are distinguished from actual GPU evidence.

## Deployment

- Windows host, Ubuntu 24.04 under WSL2; native Docker 29.1.3 and Compose 2.40.3.
- Gateway, Dashboard API/UI, two Python Worker processes, NATS JetStream,
  PostgreSQL, TiKV/PD and Qdrant were running. PostgreSQL used host port 25432
  because an existing host PostgreSQL already occupied 5432.
- Docker Desktop had stale Windows runtime socket errors. Its data and credentials
  were preserved; the working WSL Docker engine was used instead of resetting it.
- Ollama 0.35.0 used the installed custom tag `qwen3.8-9b-uc:latest` (qwen35
  architecture, 9.2B, Q4_K_M) on an RTX 4060 Laptop GPU, with all 34 layers on GPU.
  This custom tag is not a claim about an official model release name.
- The local harness used an OpenAI-compatible Ollama endpoint. For this WSL host,
  a local proxy on 11435 forwarded to Windows Ollama on 11434; no API key was needed.
- Coding ran in an isolated Linux Git fixture containing a calculator module and its
  original unittest. Production repository source was not the coding fixture.

## Actual functional results

| Surface | Observed result |
| --- | --- |
| Clean production images | Worker, Gateway and React images built; CLI/runtime import checks passed |
| Dashboard | REST/static routes, gRPC-Web task stream, live completion, history, search and UI cancellation worked |
| Offline UI | A missing backend remained offline rather than falsely connected; retries were bounded |
| Real coding | Ollama fixed subtraction to addition; independent unittest passed and original test SHA-256 was unchanged |
| Configuration | A second real distributed task used `--max-turns 12`, matching its submitted configuration |
| Task controls | Public deployed RPC pause/checkpoint/recover/resume/cancel passed; cancelled task could not resume |
| Scheduling | Real Gateway RPC rejected invalid cron and supported add/enable/disable/remove of a future test job |
| Recovery | Gateway, NATS and Worker restarts recovered connections; stored Memory and completed task state remained available |
| Memory | Real task/TiKV and project/Qdrant create/read/update/delete passed; retained keys survived Gateway restart |
| Search | Isolated repository indexing plus text, AST and hybrid retrieval returned results |
| File API | Fixture tree/file worked; missing repository returned 404; absolute/path-traversal reads returned 400 |
| NATS boundary | Incapable Worker was not dispatched; matching Worker received configuration, constraints and workflow steps |
| Event replay | Zero/positive offsets, paused child completion, advisory updates and more than 1000 events preserved checkpoint state |
| Persistence errors | A failed append was reported by checkpoint/recovery; later append attempts continued |

The completed coding task was `330840ac-243c-4676-ba87-cc14a8a5b09b`, with three
subtasks. The Dashboard now displays its completed state and 36-second duration.
The configuration retest was `d40bba02-5894-45b3-a200-2da9fd6ce284` (26 seconds).
The independent original unittest SHA-256 was
`2d66093adaf25e235bf53f8d17b2f1e10980b35e4de4979a7c7522fe0e545d5c`.

## Real inference measurement

A real streaming Ollama chat benchmark ran through the public inference runner
and passed the immutable output Oracle. The report was
`target/deployment-inference-artifacts/experiment-vu7ld87y/report.json`.

| Measurement | Value |
| --- | --- |
| Latency | 504.87 ms |
| Time to first token | 398.92 ms |
| Time per output token | 23.77 ms |
| Output throughput | 42.08 tokens/s |
| Output tokens | 7 |
| Oracle / compile check | passed / passed |

This is a warm, fixed-prompt smoke measurement, not an optimization speedup or
peak GPU memory benchmark. Earlier Oracle failures were rejected, not accepted
as performance results. No external MetaInfer service was configured, so real
MetaInfer search or kernel/runtime optimization was not executed; its HTTP
protocol, cancellation, Oracle and rollback behavior remain covered by domain tests.
Workers correctly omit `inference_infra` when that external service is unavailable.

## Fixes

1. Track Cargo.lock, use locked builds and a pinned Rust toolchain; use the official
   Bun 1.4.2 image binary so Worker builds do not depend on an installer missing unzip.
2. Make the PostgreSQL host port configurable via `UC_POSTGRES_HOST_PORT`.
3. Keep the Dashboard API's packaged native extension importable instead of
   shadowing it with a host-checkout PYTHONPATH.
4. Return HTTP 400 for invalid task payloads; preserve real JetStream duration units.
5. Treat streams as connected only after actual headers/open; reset retry budgets
   after valid messages and retain gRPC/SSE task snapshots.
6. Convert Unix-second task dates and numeric event timestamps correctly; normalize
   compact REST statuses; merge initial/refresh responses with live state instead of
   replacing it; restore cancellation from initial event history after reload; preserve
   cancellation through successful button actions,
   late failures and retries, and terminal state against partial reports/snapshots.
7. Carry complete execution configuration through Python snapshots and Rust
   restoration/upsert, including inference capability requirements and workflow steps.
8. Broadcast actual lifecycle transitions once, including parent terminal events.
9. Replay complete JetStream history with acknowledgements, serialize event writes,
   wait for durability before checkpoint/recovery, and preserve parent controls.
10. Restore readable data panels and status colors in the light theme.
11. Make the lease-renewal regression wait for actual renewal events instead of an
    unreliable 35-millisecond scheduling window.

The final rebuilt Worker/Gateway also completed a read-only Ollama task,
`f1c4535d-2c4d-4d34-a70f-3350a71a8207`, in 49 seconds. Its independent container
check still passed the original unittest and matched the original test checksum.

## Final quality results

| Check | Result |
| --- | --- |
| Python non-integration suite | 1343 passed, 1 skipped, 8 integration cases deselected |
| Python integration suite | 8 passed |
| Rust engine/types/gRPC suites with messaging | 778 passed |
| Isolated real NATS lifecycle/replay checks | 5 passed |
| Deployed Gateway controls/scheduler checks | 2 passed |
| PostgreSQL graph and merge-grant checks | 25 passed |
| Live storage integration checks | 14 passed; TiKV CRUD verified separately via deployed Gateway |
| Dashboard | production build, lint and 12 tests passed; light/dark browser inspection |
| Repository quality | Ruff, Cargo formatting, issue-flow and reference/line-ending guards passed |

The opt-in Rust live checks ran explicitly with `--ignored`; their default ignored
status is not counted as a pass. Controlled cancellation fixtures appear as Failed
in the legacy backend contract and as cancelled when their cancel event is observed
in the UI; those entries are deliberate verification history.

## Reproduce checks

Use an installed tool-capable Ollama model and start it before submitting work.
Set both `worker` and `nats-worker` to this environment in a local Compose override:

```yaml
services:
  worker:
    extra_hosts: ["host.docker.internal:host-gateway"]
    environment: &ollama
      UC_CODING_AGENT: local-harness
      UC_LLM_PROVIDER: openai
      UC_LLM_MODEL: openai/YOUR_INSTALLED_MODEL
      UC_LLM_FALLBACK_PROVIDER: none
      OPENAI_API_BASE: http://host.docker.internal:11434/v1
      OPENAI_API_KEY: ollama-local
      OPENAI_DEFAULT_MODEL: openai/YOUR_INSTALLED_MODEL
      LITELLM_LOCAL_MODEL_COST_MAP: "true"
      UC_REPO_URL: ""
  nats-worker:
    extra_hosts: ["host.docker.internal:host-gateway"]
    environment: *ollama
```

Mount an isolated Git repository at `/workspace` for both workers. Keep the local
checkout override and external Git mode separate. On Docker Desktop the host alias
normally reaches Windows Ollama; with a native WSL engine, verify that endpoint from
inside a worker and use an explicitly reachable proxy if needed. Do not confuse
Gateway storage health with successful model generation.

```bash
UC_POSTGRES_HOST_PORT=25432 docker compose   -f docker/docker-compose.yml -f YOUR_LOCAL_OVERRIDE.yml --profile app up -d --build
python scripts/verify-local-deployment.py
python scripts/verify-local-deployment.py --project verification   --task 'Read the isolated fixture and run its existing unittest without editing files.'

# The first command is read-only except for rejecting invalid submissions.
# --task explicitly submits work; never target an important checkout for a smoke edit.
cargo check --workspace --locked
cargo test -p uc-engine -p uc-types -p uc-grpc --features uc-grpc/messaging --locked

# Use a separate broker for protocol fixtures; do not publish them to the app broker.
UC_NATS_TEST_URL=nats://127.0.0.1:14222 cargo test -p uc-grpc   --features messaging --test task_snapshot_lifecycle --locked -- --ignored --test-threads=1
UC_NATS_TEST_URL=nats://127.0.0.1:14222 cargo test -p uc-engine   --features messaging --test nats_event_replay --locked -- --ignored --test-threads=1
UC_GATEWAY_TEST_ADDR=http://127.0.0.1:50051 cargo test -p uc-grpc   --test deployed_gateway --locked -- --ignored --test-threads=1

PYTHONPATH=python python -m pytest tests/python -m 'not integration' --no-cov -q
PYTHONPATH=python python -m pytest tests/python --integration -m integration --no-cov -q
cd dashboard
CI=true pnpm install --frozen-lockfile
pnpm build
pnpm lint
pnpm test
```

Windows PowerShell uses `$env:NAME='value'` for these environment variables. WSL
builds in this verification used a clean Git archive plus the modified source,
Cargo-vendored cached dependencies and local build-only registry/proxy settings
because the host context had unreadable cache ACLs and direct registry access stalled.
Those temporary caches and credentials are not part of the repository.

## Practical boundaries

- The Memory dashboard currently shows an explicit Search entry point; independent
  Memory CRUD was verified through EngineService rather than a nonexistent UI form.
- Rust owns scheduling. Its gRPC CRUD worked; the Python compatibility REST snapshot
  reports no local scheduler. No real cron job was left enabled or fired in this run.
- Hybrid retrieval used the configured hash embedding fallback; meaningful semantic
  embeddings from an external provider were not validated.
- Cross-host scaling, external Git push/merge and MetaInfer optimization require their
  corresponding deployment configuration and were not exercised on this one host.
- Docker Desktop itself remains affected by its host socket error. The local app runs
  in the working native WSL Docker engine and needs Ollama plus its local forwarding
  helper to remain running. A host reboot requires restarting these local services.
