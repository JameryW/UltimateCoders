# dsec — the DeepSeek sandbox environment

`dsec` is a named sandbox environment for the UltimateCoders Orchestrator.
It runs coding subtasks through the official **DeepSeek Harness**
(`dsh --profile headless`), using the same plugin-registry sandbox layer as
every other agent. This document is the full reference: what the environment
is, how to run it (containerized or embedded), how to diagnose it, and where
its guarantees are tested.

## 1. What dsec is

The sandbox layer is plugin-registry driven
(`python/ultimate_coders/agent/registry.py`): an *agent* is an
`AgentPluginSpec` (adapter factory, API-key env var, CLI probe), and the
*environment* is a preconfigured selection of one. `dsec` is the DeepSeek
environment:

| Concept | Value | Defined in |
|---------|-------|------------|
| Environment name | `dsec` | `python/ultimate_coders/agent/sandbox_dsec.py` |
| Agent (canonical) | `deepseek-harness` | `python/ultimate_coders/agent/harness_deepseek.py` |
| CLI contract | `dsh --profile headless "<task>"` | upstream DeepSeek Harness |
| Credentials | `DEEPSEEK_API_KEY` (required), `DEEPSEEK_BASE_URL` (optional) | env-based, no Web UI |

Selecting `dsec` is exactly equivalent to selecting `deepseek-harness` —
both build the same `dsh` request, map `api_key` to `DEEPSEEK_API_KEY`, and
advertise the `dsh` CLI probe as a worker capability. The environment name
exists so deployments, docs and tests can say "the DeepSeek sandbox" in one
word.

Because `dsec` resolves through the registry like any agent, everything that
consumes agent names works unchanged: `SandboxConfig(agent="dsec")`,
`UC_CODING_AGENT=dsec`, per-step agent overrides in multi-step workflows,
worker capability advertising, and the deny-by-default sandbox env
allowlist (the `dsec` and `deepseek-harness` allowlist entries are the same
single-sourced tuple, so they cannot drift apart).

## 2. Running it (Docker)

```bash
cp docker/.env.dsec.example docker/.env   # then set DEEPSEEK_API_KEY
./run-dsec.sh doctor                      # boot diagnosis, no API cost
./run-dsec.sh up                          # full stack, all workers on dsh
./run-dsec.sh logs worker                 # follow one service
./run-dsec.sh down
```

`run-dsec.sh` is a thin wrapper over:

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.dsec.yml \
  --profile app <action>
```

`docker/docker-compose.dsec.yml` is an override that pins
`UC_CODING_AGENT=dsec` on every agent-spawning service (`worker`,
`nats-worker`, `orchestrator`) and forwards the DeepSeek credential
variables explicitly, so a dsec deployment never depends on another agent's
key happening to be set. The worker image already ships `dsh`
(`npm install -g @deepseek-ai/dsh@0.2.0-rc.2`, see `docker/Dockerfile`), so
there is nothing extra to build.

The dashboard is served at `http://localhost:8081/dashboard` (same as the
default stack). Submit a task and the worker executes each subtask with
`dsh --profile headless` in an isolated git worktree.

### Doctor

```bash
./run-dsec.sh doctor
# inside the worker image:
docker compose ... run --rm --no-deps --build \
  -e DEEPSEEK_API_KEY -e DEEPSEEK_BASE_URL \
  worker python -m ultimate_coders.agent.sandbox_dsec --check
```

The doctor validates, with **no API cost**:

1. `DEEPSEEK_API_KEY` is present in the environment;
2. the `dsh` binary is on `PATH` and `dsh --profile headless` boots
   (`--dump-config` exercises the full plugin/credentials load path without
   dispatching a task);
3. `$DSH_HOME/.credentials.yaml` is not in the structured
   (`version:/refs:/records:`) shape that old `dsh` builds reject.

## 3. Embedding it (no Docker)

```python
from ultimate_coders.agent.sandbox_dsec import dsec_sandbox_config, preflight_dsec
from ultimate_coders.agent.sandbox import SandboxManager

ok, message = preflight_dsec()   # (True, "dsec environment ready ...") or (False, "<what to fix>")

config = dsec_sandbox_config(
    project_path="/repo",
    api_key="sk-...",              # optional; exported as DEEPSEEK_API_KEY
    max_cpu_seconds=1800,          # any SandboxConfig field can be overridden
)
manager = SandboxManager(config)
output = await manager.execute("Fix the failing test in main.rs")
```

CLI (same report the doctor prints; `--check` gates the exit code for CI):

```bash
python -m ultimate_coders.agent.sandbox_dsec            # human-readable report
python -m ultimate_coders.agent.sandbox_dsec --json     # machine-readable
python -m ultimate_coders.agent.sandbox_dsec --check    # exit 1 when not ready
```

## 4. Credentials and security posture

Agent subprocesses run with a **deny-by-default** environment allowlist
(`python/ultimate_coders/agent/sandbox.py`, T11 #653): only base system
variables, `UC_*`, proxy variables, and the selected agent's own credential
variables are inherited from the host. For dsec those are
`DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL`. An unrelated host secret
(e.g. `AWS_SECRET_ACCESS_KEY`) never reaches `dsh`.

Notes:

- `DEEPSEEK_BASE_URL` is forwarded only when explicitly set; otherwise `dsh`
  uses the public DeepSeek API.
- `DSH_HOME` is **not** in the allowlist (it controls where credentials are
  read from). To use a custom directory, widen the list explicitly with
  `UC_SANDBOX_ENV_EXTRA=DSH_HOME` — the widening is logged at worker
  startup.
- The worker container authenticates purely from the environment; no Web UI
  login or browser flow is needed.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| doctor: `dsh not found on PATH` | dsh missing from the worker image | rebuild the worker image (`./run-dsec.sh up --build`); the Dockerfile pins `@deepseek-ai/dsh@0.2.0-rc.2` |
| preflight: `dsh exits 0 but prints nothing` | the Node runtime is older than dsh's engine floor (`>= 22.19.0`): `import.meta.main` is undefined, so the CLI silently does nothing — including boot probes, which then false-positive as "OK" | upgrade Node; the worker image uses `node:22-bookworm-slim`, which tracks the current 22.x line (>= the floor). The dsec preflight detects this explicitly (`dsh --version` must print). |
| preflight: `DEEPSEEK_API_KEY is not set` | key not in `docker/.env` (or not exported) | set it in `docker/.env` / the worker environment |
| boot failure: `... must be a string` + `.credentials.yaml` | structured credentials file with an outdated `dsh` build | upgrade dsh to the pinned version, or flatten the file to a `KEY: value` map (the full repair hint is printed by preflight) |
| tasks fail with `401` | key invalid/expired, or `DEEPSEEK_BASE_URL` points at a gateway that rejects it | check the key; unset `DEEPSEEK_BASE_URL` to use the public API |
| capability `dsec` not advertised | `dsh` not on the worker's `PATH` | capability advertising probes the CLI; fix the image/PATH |

## 6. Test map

`tests/python/test_dsec_sandbox_env.py` pins the environment contract:

- **Registration** — `dsec` resolves to the DeepSeek adapter, maps
  `DEEPSEEK_API_KEY`, advertises as a capability only when `dsh` is
  probeable, registers idempotently, and shares ONE allowlist tuple with
  `deepseek-harness`.
- **Configuration** — `dsec_sandbox_config()` defaults, key injection,
  overrides, adapter wiring through `SandboxManager`.
- **Env allowlist** — a REAL subprocess through
  `SandboxManager._execute_subprocess` proves `DEEPSEEK_API_KEY` /
  `DEEPSEEK_BASE_URL` reach the child while host secrets are filtered (same
  choke-point proof style as `test_sandbox_env_allowlist.py`).
- **Adapter request** — the `dsh --profile headless "<task>"` contract and
  credential forwarding via the dsec config.
- **Preflight** — missing / booting / failing `dsh` and the
  structured-credentials hint, with a fake `dsh` on `PATH`.
- **Doctor & CLI** — report contents, key masking, `--check` exit gating,
  `--json` payload.
- **Deployment files** — the compose override pins `UC_CODING_AGENT=dsec` on
  every agent-spawning service; the env template and launcher exist.

Run them with:

```bash
PYTHONPATH=python python3 -m pytest tests/python/test_dsec_sandbox_env.py -q
```
