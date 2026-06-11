# 默认启用 Sandbox 执行模式并赋予完整权限

## Goal

将 Worker 的默认执行模式从 `"llm"` 改为 `"sandbox"`，并在 sandbox 中运行时赋予完整执行权限（网络全通、资源限制放宽），确保 coding agent 在沙箱中不会因权限不足而无法完成任务。

## What I already know

* Worker 当前默认 `execution_mode="llm"`，需显式设置 `"sandbox"` 才启用沙箱
* `SandboxConfig` 默认 `network=NetworkMode.RESTRICTED`，`max_cpu_seconds=300`，`max_memory_mb=2048`
* Rust 端 `NetworkMode` 默认也是 `Restricted`
* Docker sandbox 用 `--network=bridge` 映射 Restricted，`--network=host` 映射 Full
* SubprocessSandbox 的 `apply_unix_limits` 是空实现（仅靠 timeout）
* **当前没有命令白名单/黑名单** — sandbox 直接执行传入的 command，无限制
* "完整权限" = 网络 Full + 资源限制大幅放宽 + 无命令限制（已是现状）

## Assumptions (validated)

* "完整权限" = 网络 Full + 资源限制大幅放宽 + 允许所有命令执行
* 命令限制：当前无白名单/黑名单机制，已满足"允许所有命令"，无需额外改动
* 资源限制放宽幅度：CPU 3600s、内存 8GB、输出 50MB、文件 500MB
* 默认后端保持 subprocess（轻量开发用），Docker 可选
* 不需要新增权限模型/角色系统

## Requirements (evolving)

* Worker 默认 `execution_mode="sandbox"`
* SandboxConfig 默认 `network=NetworkMode.Full`（完整网络权限）
* Rust 端 SandboxConfig/ResourceLimits 默认值同步调整
* Python 端 SandboxConfig 默认值同步调整
* **Claude Code adapter 加 `--dangerously-skip-permissions` 标志**，跳过所有命令执行权限确认
* Codex adapter 已有 `--full-auto`，无需改动
* 所有相关测试断言同步更新

## Acceptance Criteria (evolving)

* [ ] 创建 Worker 不传 `execution_mode` 时，默认使用 sandbox 模式
* [ ] SandboxConfig 默认 network=Full
* [ ] Rust ResourceLimits 默认值大幅放宽
* [ ] Python SandboxConfig 默认值与 Rust 同步
* [ ] 所有现有测试通过

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Out of Scope (explicit)

* 新增权限角色/模型
* 修改 Sandbox trait 接口
* Docker 镜像/容器权限变更

## Technical Notes

* 涉及文件：
  - `python/ultimate_coders/agent/worker.py` — 默认 execution_mode
  - `python/ultimate_coders/agent/sandbox.py` — Python SandboxConfig 默认值 + ClaudeCodeAdapter 加 `--dangerously-skip-permissions`
  - `crates/uc-engine/src/sandbox/mod.rs` — Rust SandboxConfig/ResourceLimits/NetworkMode 默认值
  - `crates/uc-engine/src/sandbox/agents/claude_code.rs` — 加 `--dangerously-skip-permissions` 标志
  - `crates/uc-engine/src/sandbox/subprocess.rs` — 测试中的 test_config
  - `crates/uc-engine/src/sandbox/docker.rs` — 测试中的 test_config
  - `crates/uc-engine/src/sandbox/pool.rs` — 测试中的 test_config

* Claude Code CLI 权限跳过：
  - `--dangerously-skip-permissions` — 跳过所有 Bash/Read/Write 等工具的权限确认
  - 参考：https://docs.anthropic.com/en/docs/claude-code/cli-usage#dangerously-skip-permissions

* Codex CLI 已有 `--full-auto` — 全自动执行模式，无需额外改动
