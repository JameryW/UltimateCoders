# 工作目录机制：本地仓库集合或远程仓库集

## Goal

为 UltimateCoders 增加统一的"工作目录"概念：一个工作目录是一个**仓库集合**，可以是
本地的（多个本地 checkout）也可以是远程的（多个 git remote URL，按需 clone）。让 worker
启动时按工作目录定义加载仓库集、注册到 gateway、索引，search/subtask 按工作目录 scope。

## What I already know（来自勘察）

**已有但未接入的基础设施**（`repo_config.py`）：
- `uc.repos.yaml` 格式：`RepoConfig { repos: [RepoEntry{repo_id, local_path, remote_url, default_branch, tags}], scan_dirs, scan_depth }`
- `load_repos_config(path)`：CLI arg > `UC_REPOS_CONFIG` env > `./uc.repos.yaml` 自动发现
- `RepoScanner`：walk scan_dirs 发现 git repo，调 `engine.index_repo` 逐个索引
- `RepoConfigWatcher`：watchdog 热重载
- `Engine.load_repos_config` / `start_repo_watcher`（engine.py:1138-1266）：**已实现但无运行时调用方**

**缺口**：
1. 无工作目录身份字段（`workspace_id`/`name`）—— `repos` 表 + `RepoSpec` 无分组列
2. 无本地/远程模式标记 —— `RepoEntry` 无 `remote_only` 或"按需 clone"语义
3. `nats_worker.py` 不调 `load_repos_config` —— worker 启动不加载工作目录
4. `WorkspaceManager` 单 repo（`_project_path` + 单 `remote_url`）—— 不能管集合
5. `_resolve_workspace`（lsp_mcp/fs_mcp）单目录 —— MCP 文件访问/LSP 无多根
6. `search` 无工作目录 scope —— `in_all_repos()` = 全表，`in_repos([...])` 需手建列表
7. `subtask.project_id` == 单 repo_id —— 不能指向集合
8. shell 脚本不设工作目录 env

## Requirements

### MVP（最小可行）
1. **工作目录配置文件**：复用 `uc.repos.yaml`，加 `workspace_id`（或 `name`）顶层字段 + 每 `RepoEntry` 保持现有字段。支持纯本地（`local_path` 有值）、纯远程（仅 `remote_url`，无 `local_path` → worker clone 到缓存目录）、混合。
2. **接入 worker 启动**：`nats_worker.py` 启动时读 `UC_REPOS_CONFIG`（或默认 `./uc.repos.yaml`），调 `engine.load_repos_config()`，触发 scanner 索引工作目录内的所有 repo。
3. **gateway 侧工作目录 scope**：`repos` 表加 `workspace_id` 列；`list_repos` 支持 `by_workspace(id)` 过滤；`SearchQuery.in_workspace(engine, ws_id)` builder。
4. **远程 repo clone**：`RepoScanner` 对仅 `remote_url`（无 `local_path`）的 entry，clone 到 `~/.uc-cache/repos/<workspace_id>/<repo_id>` 再索引。
5. **示例配置**：提供 `uc.repos.yaml.example`（本地集合 + 远程集合各一例）。

### Out of Scope（MVP 不做）
- `WorkspaceManager` 多 repo worktree 管理（subtask 仍单 repo worktree，从工作目录里按 `project_id` 选）
- `_resolve_workspace` 多根（MCP 仍单根，由 worker 选工作目录内某 repo 作 MCP 根）
- 工作目录热重载触发 reindex（watcher 保留，但 MVP 只在启动时全量加载）
- 工作目录级权限/隔离

## Acceptance Criteria

- [x] `uc.repos.yaml` 含 `workspace_id` 字段；example 文件存在
- [x] `nats_worker.py` 启动调 `load_repos_config`，日志显示工作目录内 repo 被索引
- [x] `repos` 表有 `workspace_id` 列；`list_repos(workspace_id=...)` 过滤生效
- [x] `SearchQuery.in_workspace(engine, ws_id)` 返回该工作目录内 repo 的搜索结果
- [x] 远程-only entry 被 clone 到缓存目录并索引成功
- [x] 现有测试不破；新增工作目录加载的单元测试

> Verified: PR #208 MERGED (2026-07-02). Rust `cargo test -p uc-engine --lib`
> 334 passed; Python `test_repo_config.py` + `test_workspace.py` 28 passed.
> End-to-end (2 workers, workspace_id=e2e-test-ws) confirmed per commit 3e3144f0.

## Technical Approach

**数据模型**：
- `RepoEntry` 加 `workspace_id: Option<String>`（可选，缺省=`default`）
- Postgres `repos` 表加 `workspace_id TEXT NOT NULL DEFAULT 'default'`，加索引
- `RepoSpec`（uc-types）加 `workspace_id: String`
- `list_repos` 加可选 `workspace_id` 参数

**Python 侧**：
- `repo_config.py`：`RepoConfig` 加 `workspace_id` 字段；`RepoScanner` 传 `workspace_id` 给 `index_repo`
- `engine.py`：`index_repo` 签名加 `workspace_id`（透传到 Rust）
- `nats_worker.py`：启动时 `engine.load_repos_config(config_path)`（路径来自 `UC_REPOS_CONFIG` 或默认）
- `search/query.py`：加 `in_workspace(engine, ws_id)` → 调 `engine.list_repos(workspace_id=ws_id)` 收集 repo_ids

**远程 clone**：
- `RepoScanner._scan_one`：若 entry 无 `local_path` 但有 `remote_url`，clone 到 `~/.uc-cache/repos/<ws_id>/<repo_id>`，设 `local_path` 为该缓存路径

**Rust 侧**：
- `metadata/postgres.rs`：migration 加 `workspace_id` 列 + `list_repos_by_workspace`
- `indexer`：`index_repo` 接受 `RepoSpec.workspace_id`（透传到 register_repo）
- `EngineApi`：`list_repos` 返回值含 `workspace_id`；或加 `list_repos_in_workspace(id)`

## Decision (ADR-lite)

**Context**：需统一"工作目录"概念，支持本地集合 + 远程集合。已有 `uc.repos.yaml` 机制但未接入。
**Decision**：复用并扩展 `uc.repos.yaml`（加 `workspace_id` + 远程 clone），接入 `nats_worker` 启动，gateway 侧加 `workspace_id` 分组与 scope。不重构 `WorkspaceManager`（保持单 repo worktree，从工作目录里选）。
**Consequences**：最小改动复用现有 scanner/watcher；远程 repo 需 clone 缓存（首次成本）；MCP 多根留作未来。

## Out of Scope

- `WorkspaceManager` 多 repo worktree
- `_resolve_workspace` 多根 MCP
- 工作目录热重载 reindex
- 工作目录级权限

## Technical Notes

- 关键文件：`repo_config.py`、`engine.py:1138-1266`、`nats_worker.py:2136/800`、`metadata/postgres.rs:144/372`、`uc-types/src/index.rs:7`、`search/query.py:29`、`worker.py:1000/1088`
- 勘察报告：见此 PRD 的 What I already know 段
