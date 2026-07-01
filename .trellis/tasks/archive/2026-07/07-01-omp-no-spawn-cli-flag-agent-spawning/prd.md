# OMP --no-spawn CLI flag 禁用子 agent spawning

## Goal

主 session 里子 agent spawning 有两条独立路径：(A) OMP `task` tool spawn 子 Claude Code 进程；(B) UC `uc_task` tool LLM 分解 + 派发到 worker。用户要一个 `--no-spawn` 能力从主 session 层面彻底禁用所有 spawning。约束：vendor/oh-my-pi 是 submodule 不能改，必须在 UC 仓库内实现 + 配合 OMP 既有 `spawns:""` / `disabledAgents` 配置。

## What I already know

两条 spawning 路径（Explore 调研）：
- **Path A — OMP `task` tool**（vendor）：`vendor/.../coding-agent/src/task/index.ts:1170` `getSessionSpawns()`，空串 `""` = deny-all（line 1173/190-192）。SDK option `spawns`（sdk.ts:377,1518），CLI 默认 `"*"`（read-cli.ts:34）。UC 不读 `~/.omp/agent/config.yml`。
- **Path B — UC `uc_task` tool**（UC 控制）：`task-bridge.ts:24` 注册 `uc_task`，`case "submit"`(line 35) → `bridge.submitTask`(42) / `orchestrator.submitTask`(47)。非 LLM 入口：`extension.ts:241` `/uc submit`、`uc-rpc-server.ts:138` `submit_task` RPC。最深 chokepoint：`orchestrator.ts:230` `submitTask`（decompose 在 255）。

关键事实：
- UC 无任何既有 spawn-gating env/config（grep `UC_DISABLE/UC_NO_SPAWN/UC_NO_SUBTASK` 零命中）。
- `disabledAgents` 是 OMP-only（per-agent 粒度，非全 spawn），UC 从不读它。
- vendor 已有 `spawns: ""` deny-all 门控 —— UC 只需在 `run-omp.sh` 传参 + 设 env，不改 vendor。

## Decision (ADR-lite)

**Context**: 调研发现 OMP 主 session 的 spawns 由 SDK `CreateAgentSessionOptions.spawns`（sdk.ts:378）控制，OMP CLI 不读 `--spawns` flag/env（read-cli.ts:34 硬编码 `"*"`，仅 `omp read` 子命令用）。UC extension 作为被加载插件无法改主进程 SDK 调用。所以 Path A 无法在 UC 仓库内彻底硬禁用。
**Decision**: 分层实现——
- **Path B（UC uc_task，彻底）**：`UC_NO_SPAWN` env 在 task-bridge.ts:35 + extension.ts:241 + uc-rpc-server.ts:138 三处硬拦截，返回友好错误。这是 UC 完全可控的，彻底生效。
- **Path A（OMP task tool，软约束）**：`run-omp.sh --no-spawn` 设 `UC_NO_SPAWN=1`（覆盖 Path B）+ 文档化建议用户在 `~/.omp/agent/config.yml` 设 `task.disabledAgents` 全列表（OMP 既有软门控，task/index.ts:517/1064 过滤 agent）。不硬禁用 Path A，因为无 UC 侧硬拦截点。
- **未来若需硬禁 Path A**：需 fork vendor 改 read-cli.ts / sdk.ts 默认（out of scope）。
**Consequences**: `--no-spawn` 对 UC uc_task 是硬禁用，对 OMP task tool 是软约束（依赖用户配 disabledAgents）。文档需说清两者差异。

## Requirements

- UC 侧用 `UC_NO_SPAWN` env 作为统一开关（run-omp.sh `--no-spawn` 设它）。
- Path B 彻底拦截：`task-bridge.ts:35` `case "submit"` 顶部 + 非LLM 入口（extension.ts:241、uc-rpc-server.ts:138）查 `UC_NO_SPAWN`，返回友好错误。
- Path A 软约束：`run-omp.sh --no-spawn` 设 `UC_NO_SPAWN=1` + 打印提示（建议配 disabledAgents）。
- 禁用时给 agent/用户清晰提示，而非静默失败。
- 不改 vendor submodule。
- 提取共享 `_isSpawnDisabled()` helper 避免三处重复。

## Acceptance Criteria

- [ ] `run-omp.sh --no-spawn` 设 `UC_NO_SPAWN=1` 并打印 Path A 软约束提示。
- [ ] `UC_NO_SPAWN` 设置时，`uc_task` 的 `submit` 返回友好错误而非派发。
- [ ] `/uc submit` 与 `submit_task` RPC 在 `UC_NO_SPAWN` 时也被拦截。
- [ ] 既有 UC 工具/测试不回归。
- [ ] `--no-spawn` 不影响非 spawning 工具（uc_memory/uc_search/uc_index/uc_file/uc_worker）。

## Definition of Done

- Tests added/updated（task-bridge 拦截单测）
- Lint / typecheck / CI green
- run-omp.sh --help 文档更新（含 Path A/B 差异说明）

## Technical Approach

1. **共享 helper**：在 task-bridge.ts 或新 util 加 `_isSpawnDisabled(): boolean { return Boolean(process.env.UC_NO_SPAWN); }`。
2. **task-bridge.ts:35** `case "submit"` 顶部：`if (_isSpawnDisabled()) return {content:[{type:"text",text:"子任务派发已禁用 (UC_NO_SPAWN)。用 /uc status 查看已有任务。"}], isError:true}`。
3. **extension.ts:241** `/uc submit` + **uc-rpc-server.ts:138** `submit_task`：同样 gate，返回友好消息。
4. **run-omp.sh**：解析 `--no-spawn` → `export UC_NO_SPAWN=1` + echo 提示（Path A 软约束说明 + disabledAgents 建议）。
5. **测试**：task-bridge 拦截单测（UC_NO_SPAWN set→拒绝 + 返回 isError；unset→正常派发）。

## Out of Scope

- 改 vendor/oh-my-pi submodule 源码。
- 硬禁 Path A（需 fork vendor，未来工作）。
- worker 侧已运行 subtask（只拦截新派发）。
- 在代码里硬编码 disabledAgents 列表（仅文档化建议）。

## Technical Notes

- 关键文件：task-bridge.ts:35、extension.ts:241、uc-rpc-server.ts:138、orchestrator.ts:230、run-omp.sh:88-90,243-245。
- vendor 既有门控：task/index.ts:1170 `getSessionSpawns()`、sdk.ts:377 `spawns` option、read-cli.ts:34 CLI 默认。
- ponytail：`_isSpawnDisabled()` = `Boolean(process.env.UC_NO_SPAWN)`，一行函数，不引入 config 框架。
