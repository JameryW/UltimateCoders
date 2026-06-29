# 动态增减 worker 的 OMP 工具

## Goal

扩展 OMP `uc_worker` 工具，使其能动态**增加和减少 worker 进程实例**（docker compose scale），
并支持**从注册表强制移除**指定 worker（deregister）。让 LLM agent / 用户可在会话中按需
扩缩 worker 集群，无需手动敲 docker 命令。

## What I already know

- `uc_worker` 工具（`packages/uc-orchestrator/src/orchestrator/worker-bridge.ts`）当前只读：`list`/`status`。
- `WorkerRegistry`（`crates/uc-grpc/src/worker_service.rs`）已有 `register`/`deregister`/`heartbeat`，纯内存 map。`deregister` 只移除注册项，不停止进程。
- proto 单文件 `crates/uc-grpc/proto/engine.proto`，4 个 service。`WorkerService` 是 worker 生命周期 mutation 的归属地。
- gateway 容器（`Dockerfile.grpc` → debian:bookworm-slim）当前无 docker CLI、无 docker.sock。
- docker compose `worker` 服务（`docker-compose.yml`）支持 `--scale worker=N`，`depends_on: nats, gateway`。compose 项目名为 `docker`。
- **nats_worker 已 trap SIGTERM**（`nats_worker.py:1975`）→ `stop()` → `_deregister_from_gateway()`。scale-down 触发 SIGTERM → worker 自注销，不留 stale entry（60s heartbeat 超时为兜底）。
- TS 端 `GrpcBridge`（`grpc-bridge.ts:106-138`）当前只 wire EngineService/TaskService/DashboardService 三个 client。`WorkerService` descriptor 已在 `engine_pb.ts` 生成，加 `workerClient` 是 3 处纯 wiring。
- proto→Rust 自动 codegen（build.rs/prost）；proto→TS 手动 `buf generate`（protoc-gen-es，已在 devDeps），`engine_pb.ts` checked-in。

## Research References

- [`research/docker-scaling-from-container.md`](research/docker-scaling-from-container.md) — gateway 挂 docker.sock + compose 目录，shell-out `docker compose up -d --no-deps --scale worker=N worker`，worker 自注册/注销，registry 不直接 mutate
- [`research/scaleworkers-rpc-design.md`](research/scaleworkers-rpc-design.md) — ScaleWorkers RPC proto 草案、Rust trait 签名、TS bridge 方法、uc_worker schema 扩展

## Requirements

### proto / Rust gateway
- 在 `WorkerService` 新增 `ScaleWorkers` RPC。
- 请求支持两种语义：
  - `action="scale"` + `target_count`（声明式目标数，shell-out `docker compose up -d --no-deps --scale worker=<target> worker`）
  - `action="deregister"` + `worker_id`（委托现有 `registry.deregister()`，不停止进程——用于清理 stale/僵尸 worker）
- 响应沿用现有 `bool success` + `optional string error` + 附加 `actual_count`/`message`。
- gateway 容器装 docker CLI + 挂载 `/var/run/docker.sock` + compose 目录（或通过 env 注入 compose 文件路径 / 项目名）。
- `--no-deps` 必须传（worker `depends_on: gateway`，gateway 自己发命令会死锁）。
- compose 项目名 `--project-name docker`（env 可覆盖：`UC_COMPOSE_PROJECT`）。
- docker 调用失败（sock 不可用 / CLI 缺失 / compose 文件找不到）→ `success=false` + 清晰 error，不 panic。

### TS bridge / OMP 工具
- `GrpcBridge` 新增 `workerClient`（wire constructor + reconnect，3 处）。
- `GrpcBridge.scaleWorkers(action, opts)` 方法。
- 扩展 `uc_worker` 工具 action enum：`list` | `status` | `scale` | `deregister`。
  - `scale`：必填 `target_count`（uint）。
  - `deregister`：必填 `worker_id`。
- 工具返回可读文本（scaled N→M / deregistered X / error）。

### docker / 部署
- `Dockerfile.grpc` 装 docker CLI（apt `docker.io` 或下载静态二进制——取更小者）。
- `docker-compose.yml` gateway 服务挂载 docker.sock + compose 目录（仅 gateway profile，或统一）。
- gateway-only compose（`docker-compose.gateway.yml`）若要支持 scale，同样挂载。

## Acceptance Criteria

- [ ] `engine.proto` 新增 `ScaleWorkers` RPC + 消息，Rust codegen 通过（`cargo check -p uc-grpc`）
- [ ] gateway `ScaleWorkers` 实现：`scale` action 调 docker compose 改变 worker 实例数；`deregister` action 调 registry.deregister
- [ ] docker.sock / CLI 不可用时返回 `success=false` + error，不崩溃
- [ ] `uc_worker` 工具新增 `scale`/`deregister` action，LLM 可调用
- [ ] TS client wiring 正确（构造 + reconnect）
- [ ] scale-up 后新 worker 自注册出现在 list；scale-down 后旧 worker 自注销（SIGTERM 链路）
- [ ] 现有 `list`/`status` 行为不变
- [ ] `engine_pb.ts` 重新生成并 checked-in
- [ ] Rust + Python CI 绿（proto 改动触发 Rust CI）

## Definition of Done

- 单元测试：WorkerRegistry scale/deregister 路径（registry 层已有测试模式）
- ScaleWorkers RPC handler 有测试（mock docker 调用 或 验证 error path）
- proto + codegen + TS wiring 一致
- 文档（README / CLAUDE.md）补 uc_worker 新 action 用法
- 走 PR，CI 绿

## Out of Scope

- Kubernetes / 远程编排扩缩容
- 基于 load 的自动弹性（auto-scaling 策略）——本任务只做手动触发
- worker 进程级健康重启 / 崩溃恢复 supervisor
- docker socket proxy（tecnativa）等生产加固——记为后续
- gateway-only compose 的 scale（默认不挂 sock；如需可后补）

## Technical Approach

**执行链**：OMP `uc_worker` 工具 → `GrpcBridge.scaleWorkers()` → gRPC `WorkerService.ScaleWorkers` → gateway shell-out `docker compose --project-name docker -f <compose> up -d --no-deps --scale worker=N worker`。

**deregister** 不走 docker，直接 `registry.deregister(worker_id)`（现有 RPC `DeregisterWorker` 已存在，但为统一工具入口，ScaleWorkers 也支持 deregister action，或工具直接调 DeregisterWorker——实现时择优，倾向复用现有 DeregisterWorker RPC，工具按 action 分流）。

**安全**：docker.sock = host root 等效。本任务面向 dev/internal，默认挂载；生产加固（socket proxy）记 Out of Scope。

## Decision (ADR-lite)

**Context**: 需让 OMP 工具动态增减 worker。OMP 在宿主机、worker 在容器、gateway 是注册中心。
**Decision**: gateway 加 `ScaleWorkers` RPC，挂 docker.sock + compose 目录 shell-out docker compose scale；deregister 复用现有 registry。OMP 工具扩展 action。
**Consequences**: gateway 容器获得 host docker 控制权（安全权衡，dev 可接受）；proto/Rust/TS 三层改动；依赖 worker SIGTERM 自注销链路（已验证存在）。

## Technical Notes

- `crates/uc-grpc/proto/engine.proto` — proto 单文件
- `crates/uc-grpc/src/worker_service.rs` — WorkerRegistry + WorkerService impl
- `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` — TS gRPC clients（:106-138）
- `packages/uc-orchestrator/src/orchestrator/worker-bridge.ts` — uc_worker 工具
- `docker/Dockerfile.grpc` — gateway 镜像（需加 docker CLI）
- `docker/docker-compose.yml` — worker 服务 + gateway 挂载点
- `python/ultimate_coders/nats_worker.py:1975` — SIGTERM → deregister 链路（已验证）
