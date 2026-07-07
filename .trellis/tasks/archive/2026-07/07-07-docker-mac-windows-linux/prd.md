# PRD: Docker 适配 Mac / Windows / Linux 多运行环境

## 背景

当前 docker 配置 + run 脚本隐含假设 Mac 宿主：
- `docker-compose.yml:145` 硬编码 `/Users/jameryw/aiworks` 工作区挂载（gateway 索引本地仓库用）。
- `docker-compose.yml:139` 挂 `/var/run/docker.sock`（gateway ScaleWorkers 用）。
- `run-gateway.sh` / `run-cluster.sh` / `run-omp.sh` 用 `lsof` 探测端口，并特判 Mac Docker Desktop 的 `com.docker` 进程名。

Windows / Linux 上：硬编码路径不存在导致 gateway 无法索引本地仓库；脚本在 Windows 原生环境无 `lsof`。

## 目标

让 `docker compose` + 三个 run 脚本在 **Mac / Linux / Windows(WSL2)** 上都能正常起 gateway + storage。

## 决策（已按推荐默认定）

1. **Windows = WSL2 路径**：脚本仍 bash，Windows 用户经 WSL2 + Docker Desktop 跑。`lsof` 在 WSL 内可用。不提供原生 PowerShell 脚本，不做 OS 自适应分支。
2. **工作区挂载参数化**：compose 用 `${UC_WORKSPACE_HOST:-}``。空 = 不挂载（gateway 不索引本地仓库，纯外部 git/无索引模式）；非空 = 挂载该宿主路径（同路径进同路径出，uc.repos.yaml 无需重映射）。Mac 用户在 `.env` 填 `/Users/jameryw/aiworks`。
3. **docker.sock 保持现状**：Mac/Linux 挂 `/var/run/docker.sock`，Windows Docker Desktop 同名挂载自动映射 named pipe。不参数化 socket 路径。ScaleWorkers 三平台可用（WSL2 内 socket 行为同 Linux）。

## 范围内（MVP）

- `docker/docker-compose.yml` gateway 服务：
  - 工作区挂载改 `${UC_WORKSPACE_HOST:-}`，空时跳过（compose volume 用条件或独立 override）。
  - 提供 `docker/docker-compose.override.example.yml` 示例本地工作区挂载。
  - 新增 `docker/.env.example` 记录 `UC_WORKSPACE_HOST` 等可调变量。
- 顺手修 `docker-compose.dev.yml:47` `UC_POSTGRES_URL` → `UC_PG_URL`（与主 compose 对齐，非平台 bug 但同文件区域）。
- 文档：`CLAUDE.md` Build & Run 段补一句跨平台说明（WSL2 / 工作区挂载变量）。

## 范围外

- 原生 Windows PowerShell 脚本。
- docker swarm / k8s 跨主机编排（已有文档说明是 future work）。
- Dockerfile 平台多架构构建（已是 Linux base，三平台宿主都跑 linux/amd64 或 linux/arm64 容器，无改动需求；Mac M 系列靠 Docker Desktop 虚拟化，已 work）。
- 脚本 OS 自适应（lsof 在 WSL2 可用，不做 fallback）。

## 验收

- Mac：`./run-gateway.sh up --docker` 仍正常起 gateway + storage，gateway 能索引工作区（`.env` 填了 `UC_WORKSPACE_HOST`）。
- Linux：同上，路径用 `~/aiworks` 或自填。
- Windows WSL2：`bash run-gateway.sh up --docker` 在 WSL 内跑通，无 `lsof` 报错。
- 不填 `UC_WORKSPACE_HOST` 时 gateway 仍能起（跳过本地索引，不崩）。
