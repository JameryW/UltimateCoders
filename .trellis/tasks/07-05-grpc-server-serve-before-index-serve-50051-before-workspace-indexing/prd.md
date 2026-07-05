# PRD: serve 50051 before workspace indexing

## Problem

`docker-gateway-1` enters a zombie state: container `status=running` but
`localhost:50051` Connection refused. Workers report
`RuntimeError: transport error` on every 5s health check indefinitely.

Root cause: in `crates/uc-grpc-server/src/main.rs`, `index_workspace_repos(&engine).await`
runs **synchronously before** `Server::serve(addr)`. The function is a serial
`for req in requests { engine.index_repo(req).await }` over all repos in
`uc.repos.yaml`. When a repo (notably UltimateCoders, 4905 files) triggers a
full reindex (SHA changed → `falling back to full index`), the call blocks for
minutes. `Server::serve` is never reached → port 50051 never listens.

Docker healthcheck (`StartPeriod=15s, Retries=5`) marks the container
unhealthy after ~65s, but `restart: unless-stopped` does **not** restart
unhealthy containers (only exited ones). The process never exits (it is
blocked in async indexing, not crashed) → permanent zombie until manual
intervention.

## Goal

gRPC server listens on 50051 **within seconds** of process start, independent
of how long workspace indexing takes. Indexing runs in the background without
blocking the serve loop.

## Requirements

1. **Serve first, index after.** `Server::serve` (or an equivalent that binds
   the listener) must execute before `index_workspace_repos`. Indexing must
   not block the listener from accepting connections.

2. **Indexing in background.** `index_workspace_repos` runs as a detached
   `tokio::spawn` task. The server continues serving during indexing. Per-repo
   failures still log a warning and do not abort (current behavior preserved).

3. **Engine shared safely.** The gRPC server owns the engine; the background
   indexing task needs an engine handle. `LocalEngine` is composed entirely of
   `Arc` fields → derive `Clone` (zero-cost Arc bump) so the spawned task gets
   its own clone. No `Mutex`/`RwLock` added, no interior mutation introduced.

4. **Healthcheck reflects readiness, not indexing.** After the fix, 50051 is
   the correct readiness signal. Lower `StartPeriod` from 15s to 5s in
   `docker/docker-compose.yml` and `docker/docker-compose.gateway.yml` (serve
   is now sub-second).

5. **Restart on failure.** Add `restart: on-failure` semantics so a true
   crash (not the zombie case) recovers. Keep `unless-stopped` for the dev
   default but ensure the healthcheck window no longer masks a dead server.

## Non-goals

- Not changing indexing algorithm, parallelism, or what triggers full reindex.
- Not adding a separate readiness endpoint (TCP port check is sufficient).
- Not touching worker-side retry logic (already has exponential backoff per
  PR #220).
- Not making `index_workspace_repos` cancelable.

## Out of scope (future)

- Incremental index diff that doesn't fall back to full on SHA non-ancestor.
- Decoupling `LocalEngine` ownership from `GrpcServer` via `Arc<LocalEngine>`
  (avoids Clone, but larger refactor — not needed now).

## Verification

- `cargo check -p uc-engine -p uc-grpc -p uc-grpc-server` passes.
- `cargo test -p uc-engine` passes.
- Manual: rebuild gateway image, `./run-gateway.sh up`, observe log order —
  `listening on [::]:50051` appears **before** `Indexing workspace repos`.
- `docker exec docker-gateway-1 bash -c '</dev/tcp/localhost/50051'` succeeds
  within ~5s of container start, even while indexing logs continue.

## Risk

- `LocalEngine: Clone` — must verify no field is non-Arc-owned state. Confirmed
  all fields are `Arc<...>` (including `Arc<dyn Sandbox>`, `Arc<Mutex<TaskStore>>`).
  `start_time: Instant` is `Copy`. `config: EngineConfig` — must be `Clone`
  (verify via `cargo check`).
- Spawned indexing task outlives `main`'s other locals — it only captures the
  engine clone, which is `'static` (all-Arc). Safe.
