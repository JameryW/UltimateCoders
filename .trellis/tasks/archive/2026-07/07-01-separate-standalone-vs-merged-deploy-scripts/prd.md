# PRD: 脚本区分独立部署与合并部署

## 背景

当前 `run-omp.sh` 和 `run-cluster.sh` 只支持"合并部署"——本机跑 gRPC server + OMP，
`--docker` 时用 `docker-compose.yml` 拉起**全量存储**（TiKV/Qdrant/PG/NATS）与本机 gateway 同组。
而 `docker/docker-compose.gateway.yml` 提供了**独立 gateway 容器**（存储外接/内存 fallback），
但没有任何脚本调用它。用户要求脚本能区分两种部署模式。

## 目标

三个脚本统一支持 `--standalone` 标志切换到独立部署模式：

| 脚本 | 合并模式（现有） | 独立模式（新增 `--standalone`） |
|---|---|---|
| `run-omp.sh` | 本机 gRPC binary + OMP | 容器化 gateway + OMP 连容器 |
| `run-cluster.sh` | 本机 NATS + gRPC + workers + OMP | 容器化 gateway + 容器化 workers + OMP |
| `run-gateway.sh`（新建） | — | 专门管独立 gateway 容器：up/down/logs/env 注入 |

## 独立模式存储策略

- **默认**：不启存储容器，gateway 走内存 fallback（`compose.gateway.yml` 原设计）。
- **可选本地存储**：`--standalone --docker` 时，额外拉起 `docker-compose.yml` 的存储服务
  （pd/tikv/qdrant/postgres/nats），gateway 容器 env 指向这些本地端口。
  - 实现：gateway 容器用 host networking 或 env 注入 `host.docker.internal:2379` 等。
  - 默认走 `docker-compose.gateway.yml`；`--docker` 时用 override 文件追加存储依赖。

## 设计约束

- 不破坏现有合并模式行为（`--standalone` 缺省 = 现有行为）。
- 复用现有 `compose.gateway.yml`，不重写。
- ponytail：最短 diff，不引入新抽象。

## 验证

- `./run-gateway.sh up` → gateway 容器起来，50051 监听，内存 fallback。
- `./run-gateway.sh up --docker` → gateway + 存储容器都起来，gateway 连本地存储。
- `./run-omp.sh --standalone` → gateway 容器 + OMP。
- `./run-cluster.sh --standalone --workers 2` → gateway 容器 + 2 worker 容器 + OMP。
