# 多仓库配置与自动发现 + Dashboard 管理页

## Goal

让 UltimateCoders 支持通过 YAML 配置文件声明多个代码仓库，自动发现本地 git 仓库，watchdog 热加载配置变更，并在 Dashboard 上提供可视化的仓库管理界面。

## Requirements

### R1: repos.yaml 配置文件
- 支持 `repos` 列表显式声明仓库（repo_id, local_path, remote_url, default_branch, tags）
- 支持 `scan_dirs` 列表声明自动扫描目录
- 支持 `scan_depth` 配置扫描深度（默认 3）
- 配置文件路径优先级：CLI 参数 > `UC_REPOS_CONFIG` 环境变量 > `./uc.repos.yaml` (CWD)
- Python 侧加载 YAML，复用 `config.py` 体系，不增加 Rust 依赖
- 预留 `tags` 字段（字符串列表），供未来标签/分组功能使用

### R2: 自动发现
- 遍历 `scan_dirs` 目录，检测 `.git` 子目录识别 git 仓库
- 自动生成 repo_id（目录名或 git remote URL 推导）
- 发现新仓库后自动注册并触发索引
- 已存在索引的仓库跳过（幂等）
- 扫描深度限制生效

### R3: watchdog 热加载
- 使用 Python `watchdog` 库监听 repos.yaml 文件变更
- 配置变更后自动重新加载 + 重新扫描
- 新增仓库 → 触发索引；移除仓库 → 保留索引但不自动删除（需 Dashboard 确认）
- 监听失败时 fallback 到启动时一次性加载

### R4: Dashboard 仓库管理页
- 仓库列表：显示 repo_id、local_path、remote_url、branch、tags、索引状态、文件/符号/chunk 数量、最近索引时间
- 操作按钮：触发索引（full/incremental）、移除索引
- 自动发现面板：显示扫描到但未注册的仓库，一键添加
- tags 过滤：按标签筛选仓库列表
- 复用现有 EngineService gRPC RPCs（ListRepos, IndexRepo, GetIndexState, RemoveIndex）
- 扩展 `RepoInfo` 类型，增加 remote_url, default_branch, tags, index_counts, last_indexed_at
- 新增 `RepoManagementPanel` 组件，加入 App.tsx grid

### R5: Scheduler 集成
- repos.yaml 中的仓库可被 `scheduled_tasks.yaml` 引用
- scheduler 定时任务支持 `repo_id` 或 `tags` 过滤（如 `tags: [backend]` 索引所有带 backend 标签的仓库）
- 现有 `SchedulerConfig` 扩展 `repos` 字段

## repos.yaml 示例

```yaml
# 显式声明仓库
repos:
  - repo_id: ultimate-coders
    local_path: /home/user/projects/UltimateCoders
    remote_url: https://github.com/JameryW/UltimateCoders
    default_branch: main
    tags: [core, rust]

  - repo_id: frontend-app
    local_path: /home/user/projects/frontend-app
    remote_url: https://github.com/org/frontend-app
    default_branch: develop
    tags: [frontend, react]

# 自动扫描目录
scan_dirs:
  - /home/user/projects
  - /home/user/work

scan_depth: 3
```

## Acceptance Criteria

- [ ] repos.yaml 配置多个仓库后，引擎启动自动索引
- [ ] scan_dirs 配置后，自动发现该目录下 git 仓库并注册索引
- [ ] 修改 repos.yaml 后，watchdog 检测变更并热加载
- [ ] Dashboard 仓库列表显示所有已索引仓库及其状态（含 tags, counts, last_indexed）
- [ ] Dashboard 可触发重新索引/移除索引操作
- [ ] Dashboard 显示自动发现但未注册的仓库，可一键添加
- [ ] Dashboard 支持按 tags 过滤仓库
- [ ] 扫描深度限制生效
- [ ] watchdog 失败时 fallback 到一次性加载
- [ ] scheduled_tasks.yaml 可通过 repo_id 或 tags 引用仓库
- [ ] 并发索引协调：watchdog 触发 + 手动触发不冲突

## Definition of Done

- Tests added/updated (unit/integration where appropriate)
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Rollout/rollback considered if risky

## Decision (ADR-lite)

**Context**: 需要选择配置加载层、自动发现范围定义、热更新策略
**Decision**:
1. Python 侧加载 YAML — 复用 `config.py`，Rust 不加 YAML 依赖
2. `scan_dirs` 配置 + 环境变量兜底 — 用户显式指定扫描范围
3. watchdog 文件监听热加载 — 全自动，减少手动操作
4. 预留 tags 字段 + scheduler 引用 — 为未来标签系统铺路
**Consequences**: Python 进程需要安装 `watchdog` 依赖；热加载需处理并发索引的协调；tags 仅在配置和 UI 层存在，不影响 Rust 引擎核心

## Out of Scope

- 远程仓库自动 clone（仅索引本地已存在的仓库）
- 仓库权限控制
- Rust 侧 YAML 解析（Python 侧加载后调 Rust API）
- 仓库组/别名系统（tags 覆盖此需求）
- 跨仓库代码引用追踪

## Technical Approach

### Python 层
1. `python/ultimate_coders/repo_config.py` — 新模块：加载 repos.yaml、自动发现、watchdog
2. `RepoConfig` dataclass + `RepoEntry` dataclass（repo_id, local_path, remote_url, default_branch, tags）
3. `RepoScanner.discover(scan_dirs, scan_depth)` — 扫描 git 仓库
4. `RepoConfigWatcher` — watchdog 热加载，回调 `on_config_changed`
5. `Engine` 扩展 `load_repos_config()` / `start_repo_watcher()` 方法

### gRPC/Proto 层
- 不新增 RPC — 复用 EngineService 的 ListRepos/IndexRepo/GetIndexState/RemoveIndex
- Dashboard 前端通过 `api/endpoints.ts` 调 EngineService

### Dashboard 前端层
1. 扩展 `dashboard/src/types/dashboard.ts` — `RepoInfo` 增加 remote_url, default_branch, tags, index_counts, last_indexed_at
2. 新增 `dashboard/src/components/panels/RepoManagementPanel.tsx`
3. 扩展 `dashboard/src/hooks/useGrpcWeb.ts` — 加 indexRepo, removeIndex 方法
4. 扩展 `dashboard/src/api/endpoints.ts` — 加 indexRepo, removeIndex API

### Scheduler 集成
- `scheduler_config.py` 的 `ScheduledTask` 增加 `repo_id: str | None` 和 `tags: list[str]` 字段
- 执行时按 repo_id/tags 过滤 repos.yaml 中的仓库，对匹配的调 `engine.index_repo()`

## Implementation Plan (small PRs)

- **PR1**: Python repo_config.py + RepoScanner + 单元测试
- **PR2**: watchdog 热加载 + Engine 集成 + 单元测试
- **PR3**: Dashboard RepoManagementPanel + 类型扩展 + API 扩展
- **PR4**: Scheduler tags 引用 + 集成测试

## Technical Notes

- 配置类型: `RepoSpec` in `crates/uc-types/src/index.rs:7`
- 索引入口: `IndexPipeline.index_repo()` in `crates/uc-engine/src/indexer/mod.rs:77`
- Python 入口: `Engine.index_repo()` in `python/ultimate_coders/engine.py:322`
- Python 配置: `config.py` — 已支持 YAML/TOML 自动发现
- 调度器配置: `scheduler_config.py` — YAML 加载的先例
- gRPC proto: `EngineService` 已有 ListRepos/IndexRepo/GetIndexState/RemoveIndex RPCs
- Dashboard: 无路由，单页 hash 导航，新 panel 直接加入 grid
- 前端类型: `RepoInfo` 需扩展（加 remote_url, branch, tags, counts, last_indexed_sha）
- API 层: `api/endpoints.ts` 已有 `getRepos()` 调 EngineService
