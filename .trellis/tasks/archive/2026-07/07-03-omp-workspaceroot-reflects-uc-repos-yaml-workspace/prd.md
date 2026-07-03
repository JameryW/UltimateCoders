# OMP workspaceRoot reflects uc.repos.yaml workspace

## Goal

OMP 启动后 UI 展示的"当前工作目录"应为仓库根 `UltimateCoders`（即 uc.repos.yaml 的 workspace 根），而非 `vendor/oh-my-pi`。让 OMP 进程 cwd 与配置的 workspace 对齐，消除展示与配置脱钩。

## What I already know

- 现象：OMP UI 展示 `~/aiworks/UltimateCoders/vendor/oh-my-pi`
- 根因：`run-omp.sh:101` `cd "$SCRIPT_DIR/vendor/oh-my-pi"` 后 `exec bun packages/coding-agent/src/cli.ts` → OMP 进程 cwd = vendor/oh-my-pi
- OMP cwd 来源：`vendor/oh-my-pi/packages/utils/src/dirs.ts:175` `let projectDir = standardizeMacOSPath(process.cwd())`，`getProjectDir()` 返回它，`setProjectDir()` 可改且会 `process.chdir`
- `uc-rpc-server.ts:47,59` stubPi/stubCtx 也用 `process.cwd()` — 但那是 Python OmpBridge 的 stdio server，跟 OMP 主进程 UI 展示无关（OMP 主进程用真实 ExtensionAPI）
- `uc.repos.yaml`：workspace_id=aiworks，scan_dirs=`/Users/jameryw/aiworks`，repos=[]（auto-discover）。这是 worker/gateway 索引仓库用的，跟 OMP UI 展示完全脱钩
- `run-omp.sh` 用 `--extension ../../packages/uc-orchestrator`（相对路径，基于 vendor/oh-my-pi cwd）

## Assumptions (temporary)

- 改 OMP 启动 cwd 到 `UltimateCoders` 仓库根即可让 UI 展示正确
- `--extension` 相对路径需相应调整（改绝对路径或基于新 cwd 的相对路径）
- uc-rpc-server.ts 的 stub cwd 也应同步反映仓库根（一致性，虽不影响主 UI）

## Decision (ADR-lite)

**Context**: OMP UI 展示 cwd=vendor/oh-my-pi，与 uc.repos.yaml workspace_id=aiworks 脱钩。需让展示对齐 workspace_id=aiworks（即 ~/aiworks）。
**Decision**: 方案 3 — OMP 启动 cwd 改 ~/aiworks，run-omp.sh 自动建 symlink 把嵌套 config（UltimateCoders/.claude + vendor/oh-my-pi/.omp）暴露到 ~/aiworks/.claude + ~/aiworks/.omp，OMP 单一 cwd 发现机制经 fs.realpathSync 跟随 symlink 找到。
**Consequences**:
- ~/aiworks 成 OMP "项目根"，含 8 个子项目（OMP 当单一大项目，可接受，因 workspace 语义就是 aiworks 全集）
- 需维护 symlink（脚本 idempotent 建/修，嵌套 config 结构变则需更新脚本）
- 两个 .omp 源合并：UltimateCoders 无 .omp，vendor/oh-my-pi 有 — 无冲突
- `--extension` + 启动文件改绝对路径（cwd 不再是 vendor/oh-my-pi）

## Research References

* [`research/omp-config-dir-injection.md`](research/omp-config-dir-injection.md) — OMP 单一 getProjectDir() 无 env/flag 注入额外根；symlink 是唯一可行法（fs.realpathSync 跟随）

## Requirements

- OMP UI 展示 cwd = /Users/jameryw/aiworks（字面对齐 workspace_id=aiworks）
- run-omp.sh 启动前 idempotent 建 symlink：
  - ~/aiworks/.claude/{agents,commands,hooks,skills} → UltimateCoders/.claude/*
  - ~/aiworks/.claude/settings.json → UltimateCoders/.claude/settings.json
  - ~/aiworks/.omp/{commands,skills} → vendor/oh-my-pi/.omp/*
- OMP 启动 cwd = ~/aiworks
- `--extension` 绝对路径 $SCRIPT_DIR/packages/uc-orchestrator
- OMP 启动文件绝对路径 $SCRIPT_DIR/vendor/oh-my-pi/packages/coding-agent/src/cli.ts
- --standalone/--server/--build 各模式不受影响
- uc-rpc-server.ts stub 用 process.cwd()（启动时即 ~/aiworks，无需硬编码）

## Acceptance Criteria

- [ ] `./run-omp.sh` 启动后 OMP UI "当前工作目录" = /Users/jameryw/aiworks
- [ ] ~/aiworks/.claude symlink 指向 UltimateCoders/.claude，OMP 加载到 agents/commands/hooks/skills
- [ ] ~/aiworks/.omp symlink 指向 vendor/oh-my-pi/.omp，OMP 加载到 commands/skills
- [ ] uc-orchestrator 扩展正常加载（绝对路径）
- [ ] --standalone 模式 OMP 段同步改动
- [ ] 重复跑 ./run-omp.sh symlink 不重建/不报错（idempotent）
- [ ] ~/aiworks 无既有 .claude/.omp 被覆盖（脚本检测已有真实目录则跳过+warn）

## Implementation Plan (small PRs)

- PR1: run-omp.sh 加 symlink 建立函数 + 改 OMP 启动 cwd/路径（正常段 + standalone 段）+ 注释更新
- PR2: 验证测试（手动跑 ./run-omp.sh 确认 UI 展示 + config 加载）+ uc-rpc-server.ts 一致性确认

## Out of Scope

- 不改 OMP 上游 getProjectDir/dirs.ts/config.ts
- 不改 uc.repos.yaml 解析/扫描
- 不改 worker/gateway workspace_id 透传
- 不为 ~/aiworks 其他子项目（BestWishes 等）配 OMP

## Requirements (evolving)

- OMP UI 展示的工作目录 = 配置的 workspace 根，而非 vendor/oh-my-pi
- run-omp.sh 其余流程（--build/--server/--standalone/docker backends）不受影响

## Acceptance Criteria (evolving)

- [ ] `./run-omp.sh` 启动后 OMP UI "当前工作目录" 显示 UltimateCoders 仓库根
- [ ] uc-orchestrator 扩展仍正常加载（--extension 路径解析不破）
- [ ] --standalone / --server / --build 各模式仍工作

## Definition of Done

- Tests added/updated
- Lint/typecheck/CI green
- run-omp.sh 注释更新若行为变
- Rollback: 只动启动 cwd + 路径，回退即改回

## Out of Scope

- 不改 OMP 上游 getProjectDir 逻辑（dirs.ts）
- 不改 uc.repos.yaml 解析/扫描逻辑
- 不改 worker/gateway 的 workspace_id 透传

## Technical Notes

- `run-omp.sh:101,256` 两处 `cd vendor/oh-my-pi` + exec（standalone 段 + 正常段）
- `--extension ../../packages/uc-orchestrator` 相对 vendor/oh-my-pi
- `uc-rpc-server.ts:47,59` process.cwd() 两处（stub，一致性同步）
- getProjectDir() 是模块级 let，启动即定；setProjectDir 存在但 OMP 启动流程未必调用
