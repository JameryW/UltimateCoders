# gateway 独立容器部署（存储外接）

## Goal

让 gateway（Rust gRPC Server）能以独立 Docker 容器部署，存储后端地址通过环境变量注入，
不自动拉起 pd/tikv/qdrant/postgres/nats。适用于生产 / 外接存储场景。
保留现有 `--profile gateway` 全栈模式（含存储）作为本地开发场景。

## What I already know

- `docker/Dockerfile.grpc` 已是多阶段构建（rust:1.82-slim 构建 → debian:bookworm-slim 运行），产出 `uc-grpc-server` 二进制，EXPOSE 50051。现成可用。
- `docker/docker-compose.yml` 的 `gateway` 服务（`profiles: ["gateway","app"]`）通过 `depends_on` 强绑 pd/tikv/qdrant/postgres/nats 五个存储，并硬编码 `pd:2379` 等容器内地址。`--profile gateway up` 会把这五个存储一起拉起——不满足"单独部署"。
- gateway 启动逻辑（`crates/uc-grpc-server/src/main.rs`）所有存储均可选：
  - `LocalEngine::new` 失败 → `new_fallback()`（纯内存）
  - 无 `UC_NATS_URL` → 本地任务分解
  - 无 `UC_TASK_BACKEND=postgres` → 内存 task backend
  - 即 gateway 可零外部依赖启动（纯内存模式）。
- 存储配置 env：`UC_TIKV_PD_ENDPOINTS` / `UC_QDRANT_URL` / `UC_PG_URL` / `UC_NATS_URL` / `UC_GRPC_ADDR` / `UC_CORS_MODE` / `UC_CORS_ORIGINS` / `UC_TASK_BACKEND` / `UC_DATABASE_URL`。
- repo 已有 override 先例：`docker/docker-compose.dev.yml`（pgadmin/qdrant-dashboard 等附加 tools）。

## Requirements

- 新增一个 gateway-only compose 文件（或 profile），使 gateway 容器：
  - 不 `depends_on` 任何存储服务
  - 存储地址默认留空 / 走 env，允许纯内存启动
  - 端口 50051 映射宿主
  - 支持通过 env / `.env` 注入外部存储地址（pd/tikv/qdrant/pg/nats）
- 不破坏现有 `docker-compose.yml` 全栈开发模式（`--profile gateway` / `--profile app` 行为不变）。
- README / CLAUDE.md 补一行 gateway-only 部署用法。

## Acceptance Criteria

- [ ] 存在一种命令可单独启动 gateway 容器，且不拉起 pd/tikv/qdrant/postgres/nats
- [ ] gateway 容器在未配置任何存储地址时能成功启动（内存 fallback），监听 50051
- [ ] 通过 env 注入外部存储地址后，gateway 能连接外部 TiKV/Qdrant/PG/NATS
- [ ] 现有 `--profile gateway`（全栈）模式仍可正常工作
- [ ] 文档（README 或 CLAUDE.md）说明 gateway-only 用法

## Definition of Done

- compose 配置可被 `docker compose config` 校验通过
- 现有全栈模式回归无改动
- 文档更新
- 走 PR（feature branch），CI 绿

## Out of Scope

- 生产级镜像加固（非 root、distroless、镜像瘦身）——单独任务
- Kubernetes / 远程编排部署清单
- gateway 镜像内捆绑存储（单容器全栈）——明确不做
- TLS / mTLS gateway 传输加密

## Technical Approach

待定（见 Open Questions）：新增独立 compose 文件 vs 新增 profile/override。

## Technical Notes

- `crates/uc-grpc-server/src/main.rs` — 启动逻辑，存储全可选
- `docker/Dockerfile.grpc` — 现成镜像
- `docker/docker-compose.yml` — 现有全栈 compose（gateway 强绑存储）
- `docker/docker-compose.dev.yml` — override 先例
