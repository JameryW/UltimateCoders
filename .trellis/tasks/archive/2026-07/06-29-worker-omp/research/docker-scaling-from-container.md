# Research: Docker Scaling From Inside the Gateway Container

- **Query**: How can a containerized Rust gRPC gateway programmatically scale sibling docker-compose worker services up/down at runtime?
- **Scope**: mixed (internal compose/Dockerfile audit + external Docker mechanics)
- **Date**: 2026-06-29

> **Note on sources**: This environment exposes no web-search tool, so the external
> Docker facts below are drawn from stable, long-documented Docker/Compose v2
> behavior (unchanged since Compose v2 GA in 2022) and the project's own files.
> Anything uncertain is flagged in **Caveats**. Verify version-specific CLI flags
> against `docker compose --help` on the deployment's Compose version before
> locking the proto contract.

---

## 1. Current State (Internal Audit)

### What exists today

| File | Role |
|---|---|
| `docker/docker-compose.yml` | Full-stack dev compose. `worker` service at L169-184, `profiles: ["worker","app"]`, `depends_on: [nats (healthy), gateway (started)]` |
| `docker/docker-compose.gateway.yml` | Standalone gateway deployment (storage external). No `worker` service defined here. |
| `docker/Dockerfile.grpc` | Gateway runtime image = `debian:bookworm-slim` (L14). Installs only `libssl3 ca-certificates`. No `docker` CLI, no `docker.sock` mount. |
| `crates/uc-grpc/src/worker_service.rs` | **Passive** `WorkerRegistry` (in-memory `HashMap<String, RegisteredWorker>`). Workers self-register via gRPC; gateway never starts/stops them. |
| `crates/uc-grpc/proto/engine.proto` L36-39 | `WorkerService` RPCs: `RegisterWorker`, `WorkerHeartbeat`, `DeregisterWorker`. **No scale RPC exists.** |
| `crates/uc-grpc/Cargo.toml` | No `bollard`/`shiplift`/docker dependency present. |

### Compose project name (critical for in-container targeting)

`run-omp.sh` and CLAUDE.md invoke compose as `cd docker && docker compose ...` with **no
`-p`/`COMPOSE_PROJECT_NAME`**. Verified via `docker compose config` on this repo:
default project name = **`docker`** (Compose v2 lowercases the working-dir basename and
strips non-`[a-z0-9]`). Container naming scheme is therefore `docker-worker-1`,
`docker-worker-2`, … and service `docker_worker_1` internally.

> Implication: the gateway must pass `--project-name docker` (or read `COMPOSE_PROJECT_NAME`)
> and point `--project-directory /compose` at the compose file location, because inside the
> container the cwd will NOT be `docker/`.

### Worker service definition recap (L169-184)

```yaml
worker:
  build: { context: .., dockerfile: docker/Dockerfile }
  profiles: ["worker", "app"]
  depends_on:
    nats: { condition: service_healthy }
    gateway: { condition: service_started }
  environment:
    UC_NATS_URL: "nats://nats:4222"
    UC_GRPC_ENDPOINT: "http://gateway:50051"
    UC_SANDBOX_MODE: "subprocess"
    UC_PROJECT_PATH: "/workspace"
  command: ["python", "-m", "ultimate_coders.nats_worker", "--mode", "worker"]
```

Key facts for scaling:
- `profiles` membership means the service only runs when `--profile worker` (or `app`) is
  active. **`docker compose up` inside the container must include `--profile worker`** or
  the service is invisible to Compose.
- `depends_on: gateway: service_started` creates a **cycle risk**: if the gateway itself
  issues `docker compose up worker`, Compose sees the gateway container as a dependency of
  `worker`. This is fine for `up` (gateway is already running) but **`docker compose up`
  with `--scale` re-evaluates dependencies** — see Section 4 caveat.

---

## 2. Docker Socket Mount Approach

### Two ways to talk to Docker from inside a container

| Approach | Mechanism | Pros | Cons |
|---|---|---|---|
| **A. `docker` CLI + socket mount** | Mount `/var/run/docker.sock`, exec `docker compose ...` as a subprocess | Simple, uses exact same commands a human would type, Compose plugin handles project state | Adds ~150-200MB to image (CLI + compose plugin); subprocess + string-arg surface; need `compose` subcommand available |
| **B. Docker Engine API direct (bollard crate)** | Mount socket, speak HTTP over `/var/run/docker.sock` via Rust `bollard` crate | Pure Rust, no CLI in image, typed API, streaming events | **Cannot run `docker compose up --scale`** — Engine API has no "compose" concept; you'd have to manually create/stop/remove containers replicating the service config, re-implementing depends_on/healthcheck/profiles yourself. Much more code. |

### Recommendation: Approach A (CLI + socket)

**Rationale**: The whole point of `docker compose --scale worker=N` is that Compose owns
the service definition (env, depends_on, command, profiles). Re-implementing that over the
raw Engine API (Bollard) means the gateway must duplicate the compose YAML's worker block
in Rust — a maintenance trap. The CLI is the stable, supported interface to Compose.

The gateway does NOT need to shell out blindly; it can still use `bollard` for *read-only*
queries (list containers, inspect `docker-worker-*` health) if desired, but **scale
mutations go through the `docker` CLI**.

### Compose vs raw `docker` for scaling — use Compose

Because workers are defined in `docker-compose.yml`, the **only correct tool is
`docker compose`**. Raw `docker run` would create untracked containers that Compose cannot
later `down`/`ps`/reconcile, and would miss the `depends_on`/healthcheck/profiles wiring.

---

## 3. Add vs Scale Semantics

| | (a) `docker compose up -d --scale worker=N` | (b) `up worker` to add / `stop`+`rm` to remove one |
|---|---|---|
| **Model** | Declarative target count | Imperative per-instance |
| **Reconciliation** | Compose diffs current vs target; creates/removes only the delta | Manual; caller must track current count and pick which instance to kill |
| **Which container removed on scale-down?** | Compose removes the **highest-numbered** (`worker-N`) — deterministic | Caller chooses; risk of killing an in-use worker |
| **Race with WorkerRegistry** | Scale-down removes `worker-N` → that worker's process gets SIGTERM → its graceful-shutdown path calls `DeregisterWorker` → registry cleans up. Single source of truth (registry) stays consistent as long as workers handle SIGTERM. | Same, but caller must avoid killing a worker mid-subtask; no backpressure signal. |
| **Concurrent calls** | Compose serializes via project state; safe to call repeatedly | Two concurrent `rm` calls can target the same instance |
| **Handling `depends_on`** | Re-evaluated; `gateway` already `started` so it's a no-op | Same |

### Recommendation: (a) declarative `--scale`

**Exact commands the gateway should run** (from inside the container, with compose file at
`/compose/docker-compose.yml` and project name `docker`):

```
# Scale to target count N (idempotent — safe to call with same N)
docker compose --project-name docker \
  --project-directory /compose \
  -f /compose/docker-compose.yml \
  --profile worker \
  up -d --scale worker=N --no-deps worker

# List current worker containers (read-only, for computing current N)
docker compose --project-name docker \
  -f /compose/docker-compose.yml \
  --profile worker \
  ps --format json worker

# Force-remove a specific drifted instance (rare, for GC)
docker rm -f docker-worker-3
```

**Why `--no-deps`**: the `worker` service declares `depends_on: gateway`. When the gateway
container itself issues this command, Compose would otherwise try to (re)create/ensure the
`gateway` service — a no-op at best, a deadlock at worst (Compose waiting on a container
that is the one issuing the command). `--no-deps` skips dependency reconciliation.

**Scale-down behavior**: Compose sends SIGTERM to the removed `worker-N` container. The
worker's Python entrypoint (`ultimate_coders.nats_worker`) must trap SIGTERM, finish/drain
in-flight NATS subtasks, call `DeregisterWorker`, then exit. This already aligns with the
existing `DeregisterWorker` RPC path — no new worker-side code needed for scale-down, only
a signal handler (verify one exists; see Caveats).

**Scale-up behavior**: new `worker-N` container starts → runs
`python -m ultimate_coders.nats_worker --mode worker` → connects NATS → calls
`RegisterWorker` → registry gains the worker. Also uses the existing RPC path. **The
gateway does NOT need to "add" workers to the registry; they self-register.** The scale RPC
only needs to ask Compose for N replicas and then *optionally* wait until registry
`available_count` reaches N (or timeout).

---

## 4. `docker compose up -d --scale` on an already-running project — does it work?

**Yes.** `docker compose up -d --scale worker=N` is explicitly designed for this: it is a
reconcile operation. If 3 are running and you ask for 5, Compose creates `worker-4`,
`worker-5` and leaves 1-3 untouched. If you ask for 2, it stops+removes `worker-3`.
Already-running instances are not restarted (unless their config changed). Verified Compose
v2 behavior; the repo's Compose is v5.1.4 (`docker compose version` → v5.1.4).

**Profile caveat (important)**: the `worker` service is under `profiles: ["worker","app"]`.
A plain `docker compose up worker` with no `--profile` will error
`no such service: worker`. The gateway **must** pass `--profile worker` on every invocation.
If the stack was originally started with `--profile app`, both profiles are active and
`--profile worker` alone is still sufficient to address the `worker` service (profiles are
additive at invocation time, not stored).

**`depends_on` cycle caveat**: `worker` depends on `gateway`. When the gateway runs `up
--scale worker`, Compose resolves `gateway`'s state. Since the gateway is already running
and `condition: service_started` is satisfied, this is a no-op — **but only because we add
`--no-deps`**, which skips dependency resolution entirely. Without `--no-deps`, Compose may
attempt to inspect/recreate the `gateway` service, which is risky when the caller IS the
gateway. Always use `--no-deps worker`.

---

## 5. Getting `docker` CLI into `debian:bookworm-slim`

### Minimal install

The `docker-ce-cli` package alone (no daemon, no `containerd`) is the smallest path. Two
options:

**Option 1 — official Docker APT repo (recommended, versioned)**
```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg libssl3 \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*
```
- `docker-ce-cli` + `docker-compose-plugin` = the CLI + `docker compose` subcommand. **No
  `dockerd`** — the daemon stays on the host; the container talks over the mounted socket.
- Image size impact: `docker-ce-cli` ≈ 90MB, `docker-compose-plugin` ≈ 60MB (Go static
  binary). Adds ~150MB to the ~120MB bookworm-slim base → ~270MB total. Acceptable for a
  gateway image.

**Option 2 — copy just the two static binaries (smallest, no APT repo)**
```dockerfile
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker/compose-bin:v2.29.2 /docker-compose /usr/libexec/docker/cli-plugins/docker-compose
```
- `docker:27-cli` is a dedicated CLI-only image; copying the two binaries avoids the APT
  layer entirely. ~150MB added but no apt cache, single layer. Cleanest for reproducible
  builds. **Recommended for this project** (matches the existing minimal-bookworm style and
  avoids adding Docker's APT repo to the image's trust set).

> The existing `Dockerfile.grpc` runtime stage already runs `apt-get install libssl3
> ca-certificates`. Either option above layers on top cleanly.

### socket mount + compose file mount (compose additions)

```yaml
# in docker-compose.gateway.yml (and/or docker-compose.yml) gateway service:
  gateway:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro   # ro where possible — see §6
      - ../docker:/compose:ro                          # compose file readable inside
    environment:
      UC_DOCKER_COMPOSE_FILE: "/compose/docker-compose.yml"
      UC_DOCKER_PROJECT_NAME: "docker"
      UC_DOCKER_PROFILE: "worker"
```
- Mounting the **compose file dir** (`/compose`) read-only lets the gateway invoke
  `docker compose -f /compose/docker-compose.yml` without baking the YAML into the image
  (which would drift from the host's actual compose file).
- `:ro` on the socket is *not* honored by Docker for write operations (the socket is a
  bidirectional Unix socket; `:ro` only blocks the file metadata, not writes through it).
  See Section 6 — real write protection needs a socket proxy.

---

## 6. Security: docker.sock = host root

Mounting `/var/run/docker.sock` into any container grants **full root on the host**: a
container with socket access can `docker run --privileged -v /:/host ...` and trivially
escape. For a dev/internal tool this is the standard, accepted trade-off, but it must be a
deliberate choice.

### Mitigation options (ranked by pragmatism for this project)

| Option | What it does | Fit for UltimateCoders |
|---|---|---|
| **1. Raw socket mount (status quo approach)** | Full trust; gateway can do anything Docker can | **OK for dev/internal-only** behind a trusted network. Document the risk. Simplest path to a working ScaleWorker RPC. |
| **2. `tecnativa/docker-socket-proxy`** | Sidecar container exposing the Engine API over HTTP with per-endpoint allow/deny (e.g. allow `/containers` POST, deny `/exec`, deny `/images`). Gateway talks HTTP to proxy, never raw socket. | **Recommended hardening** if this ever leaves the dev box. Adds one sidecar container + an env-var-configured HTTP client in Rust. Blocks the most dangerous escape vectors (`/exec`, `attach`, privileged create). The downside: `docker compose` CLI cannot speak to the proxy (it needs the raw socket) — so a proxy forces Approach B (Bollard + raw Engine API), losing Compose semantics. **This re-opens the §3 problem.** |
| **3. Rootless Docker / Podman** | Runs dockerd as non-root user; socket compromise ≠ host root | Changes the host Docker setup; out of scope for a project that ships a `docker-compose.yml` expecting standard Docker. |
| **4. Capability restrictions on the gateway container** | Drop `--cap-add`, run `--user 1000` | Does NOT help: the Docker socket is a capability-free RPC channel; any UID with write access to the socket controls the daemon. |

### Pragmatic recommendation

For **dev/internal** (the current deployment shape — `docker-compose.yml` dev stack, no
production hardening elsewhere in the repo): **Option 1, raw socket mount**, with:
- A clear comment in the compose file that this grants host-root equivalence.
- The gateway binds the scale RPC behind an explicit feature flag / env var
  (`UC_ENABLE_DOCKER_SCALING=true`, default false) so the capability is opt-in and absent
  from the standalone `docker-compose.gateway.yml` unless explicitly enabled.
- No network exposure of the gateway's gRPC port to untrusted networks without the flag off.

If/when this ships to a shared environment, switch to **Option 2 (socket proxy)** and
accept the cost of re-implementing scale via the raw Engine API (Bollard): the gateway would
issue `ContainerCreate`/`ContainerStart`/`ContainerStop`/`ContainerRemove` against
`docker-worker-N`, reading the template config from the compose file itself. That is more
code but is the only way to get real least-privilege. Defer until needed.

---

## 7. Concrete Recommendation

### Approach
**`docker` CLI + `docker compose` subprocess, mounted socket + mounted compose file.**
Declarative `--scale worker=N`. No Bollard for mutations (keep it as an optional future
read-path only).

### Dockerfile change (`docker/Dockerfile.grpc`, runtime stage)
Replace the runtime stage with one that adds the two static binaries (Option 2 from §5):

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libssl3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker/compose-bin:v2.29.2 /docker-compose \
     /usr/libexec/docker/cli-plugins/docker-compose
COPY --from=builder /app/target/release/uc-grpc-server /usr/local/bin/uc-grpc-server
ENV RUST_LOG=info
EXPOSE 50051
ENTRYPOINT ["uc-grpc-server"]
```

### Compose change (gateway service, in `docker-compose.gateway.yml` and optionally `docker-compose.yml`)
```yaml
  gateway:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./docker:/compose:ro          # path relative to repo root when using -f docker/docker-compose.yml
    environment:
      UC_DOCKER_COMPOSE_FILE: "/compose/docker-compose.yml"
      UC_DOCKER_PROJECT_NAME: "docker"
      UC_DOCKER_PROFILE: "worker"
      UC_ENABLE_DOCKER_SCALING: "true"
```
> The `./docker` mount source must resolve from wherever `docker compose` is invoked. Since
> `run-omp.sh` does `cd docker && docker compose up`, a relative `./docker` from there is
> `docker/docker` — wrong. Use an absolute host path or mount `..` (repo root) and set
> `UC_DOCKER_COMPOSE_FILE=/repo/docker/docker-compose.yml`. **Flag for the implementer to
> pin down** — see Caveats.

### The ScaleWorker RPC shape (suggested proto)
```proto
service WorkerService {
  rpc RegisterWorker(...) returns (...);
  rpc WorkerHeartbeat(...) returns (...);
  rpc DeregisterWorker(...) returns (...);
  rpc ScaleWorkers(ScaleWorkersRequest) returns (ScaleWorkersResponse);  // NEW
  rpc GetWorkerScale(GetWorkerScaleRequest) returns (GetWorkerScaleResponse); // NEW (read current count)
}

message ScaleWorkersRequest {
  uint32 target_count = 1;   // desired N; gateway reconciles to this
  optional uint32 timeout_secs = 2;  // wait until registry available_count >= target_count (best-effort)
}
message ScaleWorkersResponse {
  bool success = 1;
  uint32 previous_count = 2;
  uint32 current_count = 3;
  optional string error = 4;
}
```

### Exact commands the Rust impl should shell out
```
docker compose \
  --project-name "$UC_DOCKER_PROJECT_NAME" \
  --project-directory "$(dirname "$UC_DOCKER_COMPOSE_FILE")" \
  -f "$UC_DOCKER_COMPOSE_FILE" \
  --profile "$UC_DOCKER_PROFILE" \
  up -d --no-deps --scale worker=<N> worker
```
Read current count:
```
docker compose --project-name docker -f /compose/docker-compose.yml --profile worker \
  ps --format json worker
```
(parse the JSON array length; `--format json` returns one JSON object per line in Compose v2.)

### Reconciliation with WorkerRegistry (avoids races)
1. Gateway receives `ScaleWorkers{target_count: N}`.
2. Shell out `docker compose up -d --no-deps --scale worker=N worker`.
3. **Do not** mutate `WorkerRegistry` directly — workers self-register/deregister via the
   existing gRPC RPCs on container start/stop.
4. Optionally poll `registry.available_workers().len()` until `>= N` or `timeout_secs`
   elapses; return current count either way. This makes the RPC synchronous-feeling while
   keeping the registry the single source of truth.
5. For scale-down, rely on the removed containers' SIGTERM → worker graceful-shutdown →
   `DeregisterWorker`. Add a stale-heartbeat sweep (already present:
   `WorkerRegistry::stale_worker_ids()`, 60s timeout) as a backstop for workers that die
   without deregistering.

---

## 8. Related Specs / Tasks

- `.trellis/tasks/06-29-gateway/prd.md` — notes `docker-compose.gateway.yml` override
  precedent and the gateway/storage decoupling; this scaling work layers on top.
- `.trellis/tasks/archive/2026-06/06-28-implement-distributed-execution-full-chain/prd.md`
  L32 — mentions `--scale worker=N` + worker auto-register capabilities (the existing
  passive model this RPC would actively drive).

---

## Caveats / Not Found

- **No web search tool available** in this environment. All external Docker facts (Compose
  `--scale` reconcile semantics, `--no-deps` behavior, `docker:27-cli` binary path, image
  sizes) are from stable documented behavior; **verify the exact `docker:27-cli` and
  `docker/compose-bin` image tags/paths against Docker Hub** before copying binaries — the
  `/docker-compose` path inside `docker/compose-bin` and the
  `/usr/libexec/docker/cli-plugins/docker-compose` install location are correct as of
  Compose v2.20+ but confirm on the tag you pin.
- **Compose v5.1.4 is unusual** (Compose plugin versions are normally `v2.x`). The repo's
  `docker compose version` reports `v5.1.4` — possibly a Docker Desktop / custom build.
  Re-test the exact `ps --format json` output shape on this version before parsing it in
  Rust.
- **Worker SIGTERM handler**: this research assumes `ultimate_coders.nats_worker` traps
  SIGTERM and calls `DeregisterWorker` before exit. **NOT verified** — a grep for signal
  handlers in `python/ultimate_coders/` was not run. If no handler exists, scale-down will
  leave stale registry entries until the 60s heartbeat timeout. The implementer must confirm
  or add one.
- **`depends_on` cycle with `--no-deps`**: `--no-deps` is the documented escape hatch, but
  on some Compose versions `up --scale <svc> --no-deps <svc>` still warns about
  dependencies. Test on v5.1.4 specifically.
- **Host-side compose-file mount path**: the exact relative-vs-absolute mount source depends
  on whether the operator runs `docker compose` from `docker/` or the repo root. The
  implementer must pin this (recommend mounting the repo root and using an absolute
  `UC_DOCKER_COMPOSE_FILE`).
- **`docker compose up --scale` with `--profile`**: confirmed behavior is that the profile
  must be active; verify that passing `--profile worker` to a project originally started
  with `--profile app` does not drop the app-profile services (it should not — profiles are
  per-invocation, not project-state — but confirm on v5.1.4).
