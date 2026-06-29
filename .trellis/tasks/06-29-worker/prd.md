# worker 容器化共享存储部署

## Goal

让所有 worker 以容器化方式部署，并共享存储——既共享存储后端（TiKV/Qdrant/PG/NATS），
也共享**统一 git 仓库管理系统**。worker 将来是**分布式集群**（跨主机），因此需要一个
中心化 git 服务做统一仓库管理：worker 从中心仓库获取代码、在本地 worktree 隔离执行、
push 回中心仓库，由中心仓库统一管理 + 合并仲裁。使 PR #192 的 ScaleWorkers 真正可用、
多 worker 跨主机协作产出可汇聚。

## Direction (user-confirmed)

- worker 将来是**分布式集群**（跨主机），非单机。
- 需要一个 **git 系统做统一仓库管理**——bind mount 宿主代码的单机方案不符合目标。
- 这是基础设施级子系统设计，需 research-first + 架构对齐。

## What I already know

- worker 容器化已存在：`docker-compose.yml` 的 `worker` 服务（`profiles: ["worker","app"]`，`--scale worker=N`）。
- worker 容器当前 `UC_PROJECT_PATH=/workspace` 但 **未挂载任何 volume**——`/workspace` 是空的。
- 存储后端（TiKV/Qdrant/PG/NATS）已通过 docker compose 共享。✅
- **WorkspaceManager**（`agent/workspace.py`）已有 **git worktree 隔离**：`acquire` 建分支 + worktree，`release` merge 回 base_branch、冲突时保留分支。**纯本地**，无 fetch/push/clone 到中心仓库。
- 已有 `DistributedConflictDetector` + `uc.file.changed` / `uc.memory.changed` NATS 广播——多 worker 协调机制已设计，但缺中心 git 源。
- `remote_url` 概念已存在（`repo_config.py`/`engine.py` 的 `index_repo`），但仅用于 repo_id 派生，**不用于 worker 代码同步**。
- sandbox backend：`subprocess`（默认）或 `docker`。
- PR #192 ScaleWorkers scale action 要求 worker 是 compose 管理的容器。
- **无中心 git 服务集成**（无 Gitea/Gogs/GitLab/bare repo 管理）——需新建。

## Open Questions (blocking)

- 统一 git 系统形态：自建 Gitea 容器 vs 裸 bare repo + git 协议 vs 对接外部 GitHub/GitLab vs 自研轻量 git 服务。
- worker 代码同步模型：每 subtask clone-on-demand vs 共享持久 clone + fetch vs worktree-only。
- 合并仲裁：中心仓库直接收 push（last-write-wins / PR 式 review）vs gateway 仲裁。
- 范围：本任务做到哪一步（MVP 是"中心 git 服务 + worker 容器挂载共享 clone"，还是完整合并仲裁）。

## Requirements (evolving)

- 中心 git 仓库管理系统（统一仓库源）。
- worker 容器从中心仓库获取代码，本地 worktree 隔离执行，结果回传中心仓库。
- 多 worker 跨主机并发，冲突检测 + 合并协调生效。
- ScaleWorkers 在此部署下可真正增减 worker 实例。
- 不破坏现有宿主机进程部署模式。
- 文档说明分布式 worker + 统一 git 部署用法。

## Acceptance Criteria (evolving)

- [ ] 中心 git 服务可用，worker 能从其获取代码
- [ ] 多 worker 容器（可跨主机）并发执行 subtask，worktree 隔离 + merge 回中心仓库
- [ ] 跨 worker 文件冲突可检测
- [ ] ScaleWorkers scale up/down 在分布式部署下生效
- [ ] 现有宿主机部署模式不受影响
- [ ] 文档更新
- [ ] CI 绿

## Out of Scope

- Kubernetes 部署（本任务用 docker，但设计需兼容未来 k8s）
- worker 容器内 Docker-in-Docker sandbox（保持 subprocess backend）
- 存储后端本身的容器化（已具备）
- 完整 CI/CD 流水线（中心 git 系统只做仓库管理，不含 CI）

## Research References

- [`research/external-git-sync-model.md`](research/external-git-sync-model.md) — 推荐同步模型 (b) 持久 clone + fetch/push；结果模型 (c) worker push subtask 分支、gateway/aggregator merge；PAT+GIT_ASKPASS；WorkspaceManager additive 改造

## MVP Scope (本任务 = 阶段 1：worker 容器化 + 外部 git 同步基础)

研究挖出 3 个硬阻塞，本阶段先打通基础链路：
1. `git` 未装在 worker 运行镜像（`Dockerfile` 只装 libssl3/ca-certs/curl）→ 装 git。
2. WorkspaceManager 纯本地 worktree，无 remote 同步 → 加 `ensure_clone`/`fetch_on_acquire`/`push_on_release`，分支 off `origin/<base_branch>`。
3. worker 容器 `/workspace` 无 volume → 挂持久 volume 存 clone + 凭证 env。

本阶段交付：worker 容器能从外部 GitHub/GitLab clone 仓库、worktree 隔离执行 subtask、push 回 `uc/subtask/<id>` 分支。

## 阶段 2/3 (后续任务，本任务 Out of Scope)

- 阶段 2：合并仲裁——Python aggregator/Orchestrator 拉 subtask 分支，用 ConflictResolver merge 进 main 并 push。
- 阶段 3：ScaleWorkers 分布式端到端验证 + DistributedConflictDetector 降级为 advisory hint + 文档。

## Technical Notes

- `python/ultimate_coders/agent/workspace.py` — WorkspaceManager git worktree（纯本地，需扩展远程同步）
- `python/ultimate_coders/agent/distributed_conflict.py` — DistributedConflictDetector
- `python/ultimate_coders/nats_worker.py` — file_changed / memory_changed 订阅
- `python/ultimate_coders/repo_config.py` — remote_url 概念（仅 repo_id 派生）
- `docker/docker-compose.yml` — worker 服务（无 volume，待补）
- PR #192 — ScaleWorkers（依赖容器化 worker）
