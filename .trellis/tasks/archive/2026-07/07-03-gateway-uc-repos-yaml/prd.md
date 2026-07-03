# 容器化 gateway 加载 uc.repos.yaml 工作目录

## Goal

PR #211 已让 Rust gRPC server 在启动时加载 `uc.repos.yaml` 并索引工作目录 repo，代码已
合并入 main（`5650c139`）。但用户的运行形态是**容器化 gateway**（`docker-gateway-1`，
镜像 `docker-gateway`），4 小时前启动的旧镜像不含 PR #211 代码，且容器内没有
`uc.repos.yaml` / 看不到宿主机 `~/aiworks`。所以 OMP 仍感知不到配置的工作目录。

本任务把 PR #211 的修复落地到容器化部署：重建镜像 + 挂载配置与 repo 源 + 设
`UC_REPOS_CONFIG`，让容器内 gateway 启动时加载 `uc.repos.yaml` 并索引 `~/aiworks` repo。

## What I already know（已勘察）

**当前部署形态**（`docker ps`）：
- `docker-gateway-1` 跑 `docker-gateway` 镜像，监听 `0.0.0.0:50051`，4 小时前启动（PR #211 前）
- 用 `docker/docker-compose.yml` 的 `gateway` 服务（profile `gateway`/`app`）
- 存储容器同栈：pd/tikv/qdrant/postgres/nats 全 healthy

**compose gateway 服务现状**（`docker/docker-compose.yml:108-160`）：
- 已挂 `../:/app/docker-compose-root:ro`（仓库根只读 → 容器 `/app/docker-compose-root`）
- 已挂 `/var/run/docker.sock`（ScaleWorkers）
- environment 设了存储地址，**没设 `UC_REPOS_CONFIG`**
- **没挂 `~/aiworks`** → 容器内看不到 `uc.repos.yaml` 里 `scan_dirs: [/Users/jameryw/aiworks]` 的源

**Dockerfile.grpc**：WORKDIR `/app`，ENTRYPOINT `uc-grpc-server`。
- server 启动时 CWD=`/app`，相对路径 `./uc.repos.yaml` 找不到（配置在 `/app/docker-compose-root/uc.repos.yaml`）
- → 必须设 `UC_REPOS_CONFIG=/app/docker-compose-root/uc.repos.yaml`

**uc.repos.yaml**（仓库根，已 gitignore，机器特定）：
- `workspace_id: aiworks`，`scan_dirs: [/Users/jameryw/aiworks]`，`scan_depth: 2`
- scan_dirs 是宿主机绝对路径 → 容器内需同路径挂载（`~/aiworks:/Users/jameryw/aiworks:ro`）

**本地 main 停在 #207**（`3843fb6a`），未 pull PR #211 —— 重建镜像前需 `git pull origin main`。

## Requirements

### MVP
1. **本地 main 拉最新**：`git pull origin main`（含 PR #210 + #211）。
2. **compose gateway 服务加挂载**：
   - `~/aiworks:/Users/jameryw/aiworks:ro`（让容器看到 scan_dirs 的 repo 源，只读）
   - 已有的 `../:/app/docker-compose-root:ro` 保留（uc.repos.yaml 在其中）
3. **compose gateway 服务加 env**：`UC_REPOS_CONFIG: "/app/docker-compose-root/uc.repos.yaml"`
4. **重建镜像**：`docker compose -f docker/docker-compose.yml --profile gateway build gateway`
   （`run-gateway.sh` / `run-cluster.sh` 等价命令亦可）
5. **重启 gateway 容器**：`docker compose -f docker/docker-compose.yml --profile gateway up -d gateway`
6. **验证**：容器日志出现 `Indexing workspace repos from uc.repos.yaml workspace_id=aiworks`；
   `Engine(mode='grpc').list_repos()` 返回 `~/aiworks` 下的 repo，带 `workspace_id=aiworks`。

### Out of Scope（MVP 不做）
- 改 `uc.repos.yaml` 的 `scan_dirs` 路径（保留宿主机绝对路径，靠 volume 挂载同路径映射）
- standalone `docker-compose.gateway.yml` 的同步改动（用户用的是 `docker-compose.yml` 的 gateway；
  standalone 那个的挂载留作后续，需时再补）
- 重建镜像的 CI 自动化（本任务手动 `docker compose build`）
- 远程-only repo 的容器内 clone（Python worker 模式才需要，gateway 本地场景不需要）

## Acceptance Criteria

- [x] 本地 main 含 PR #211 commit `5650c139`
- [x] `docker/docker-compose.yml` gateway 服务加 `~/aiworks` 挂载 + `UC_REPOS_CONFIG` env
- [x] gateway 镜像重建（含 PR #211 代码）
- [x] gateway 容器重启后日志含 `Indexing workspace repos ... workspace_id=aiworks`
- [x] `Engine(mode='grpc').list_repos()` 返回 `~/aiworks` repo，全带 `workspace_id=aiworks`
- [x] 现有存储容器（pd/tikv/qdrant/postgres/nats）不受影响，仍 healthy

> Verified end-to-end on Colima runtime (Docker Desktop replaced): rebuilt
> docker-gateway image with PR #211, started full stack. Container log:
> `Indexing workspace repos from uc.repos.yaml workspace_id=aiworks total=7`
> → `Workspace repo indexing complete indexed=7 total=7`. gRPC query
> `Engine(mode='grpc').list_repos()` returned 7 repos all `workspace_id=aiworks`;
> `list_repos(workspace_id='aiworks')` filtered to 7. All 5 storage containers
> healthy.

## Technical Approach

1. `git pull origin main`（或 `git checkout main && git pull`）。
2. 编辑 `docker/docker-compose.yml` 的 `gateway` 服务：
   - `volumes:` 加 `- ~/aiworks:/Users/jameryw/aiworks:ro`（或用绝对路径
     `/Users/jameryw/aiworks:/Users/jameryw/aiworks:ro`，compose 会展开 `~`）
   - `environment:` 加 `UC_REPOS_CONFIG: "/app/docker-compose-root/uc.repos.yaml"`
3. 重建：`docker compose -f docker/docker-compose.yml --profile gateway build gateway`
4. 重启：`docker compose -f docker/docker-compose.yml --profile gateway up -d gateway`
5. 验证日志 + Python client 查 list_repos。

## Decision (ADR-lite)

**Context**：PR #211 代码正确但容器镜像旧 + 容器看不到配置/源。
**Decision**：volume 挂载 `~/aiworks`（只读）+ 设 `UC_REPOS_CONFIG` 指向已挂载的仓库根内
配置。保留宿主机绝对路径（同路径挂载，零映射开销）。
**Consequences**：宿主机路径 `~/aiworks` 硬编码进 compose（机器特定，但 compose 本就是
本地 dev 用）；只读挂载安全；standalone gateway.yml 不同步（用户没用它）。

## Out of Scope

- standalone `docker-compose.gateway.yml` 改动
- CI 自动重建镜像
- 远程-only repo 容器内 clone
- 改 uc.repos.yaml 的 scan_dirs 路径

## Technical Notes

- 关键文件：`docker/docker-compose.yml:108-160`（gateway 服务）、`docker/Dockerfile.grpc`（WORKDIR /app）、
  `uc.repos.yaml`（仓库根，scan_dirs=/Users/jameryw/aiworks）
- spec：`.trellis/spec/backend/workspace-config-spec.md`（加载契约 + resolution order）
- PR #211 commit：`5650c139`（origin/main）
