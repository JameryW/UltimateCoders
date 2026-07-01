# 优化 worker LLM 错误信息展示

## Goal

503 等瞬时 LLM 错误重试耗尽后，用户看到 `⚠ Execution error: 503 The system is busy...` —— 既不说明已重试次数，也不区分瞬时/永久错误，纯堆原始 provider 串。优化错误信息，让用户一眼看出：错误类型（瞬时/永久）、是否重试过、重试几次、根因摘要。

## What I already know

错误展示链路（Explore 调研）：
- **Origin** `llm.py:632/668` — 重试耗尽后 `raise` 原始异常，无包装、无重试上下文。
- **Worker** `worker.py:767` — `summary=f"Execution error: {e}"`，加通用前缀，是所有下游 UI 的错误源。`SubtaskResult.error` 字段（types.py:78）从未被设置。
- **NATS** `nats_worker.py:1500` — `data["error"]=result.summary[:300]`；`_make_task_update_payload:90` 检查 `st.result.error` 但永远空 → error 字段缺失，错误文本塞在 `result` 字段里。
- **gRPC→OMP** Rust `conversions.rs:789` proto `result` 回退到 summary；TS `grpc-bridge.ts:693` 映射到 `TaskSync.subtasks[].result`；`orchestrator.ts:1346` `result.error=remoteSubtask.result`。
- **UI** 4 处渲染：status-formatter(60字)、progress-widget(只显示 ID 不显示错误文本)、subtask-tree(width-12)、task-result-renderer(70字)、orchestrator.showStatus(全长)。

## Requirements

- 错误信息包含：错误类别（瞬时/永久）、是否重试、重试次数、根因摘要。
- 瞬时错误（503/429/529/overloaded/server_error 等）与永久错误（400/401/invalid key）视觉区分。
- `SubtaskResult.error` 字段被正确设置（修复"永远空"的 bug），让下游消费者能用。
- UI 各渲染点显示友好错误，而非裸 provider 串。
- 不破坏现有错误字段契约（summary 仍可读）。

## Acceptance Criteria

- [ ] 503 重试耗尽后，UI 显示含"瞬时错误"+"重试 N 次"的提示，而非裸 `Execution error: 503...`。
- [ ] 400/401 等永久错误显示"永久错误"标识，不误导为可重试。
- [ ] `SubtaskResult.error` 在失败路径被设置（单测覆盖）。
- [ ] progress-widget 失败时显示首条错误摘要（不只 ID）。
- [ ] 既有 worker/llm/UI 测试不回归。

## Definition of Done

- Tests added/updated
- Lint / typecheck / CI green
- 不改变错误字段的数据契约（只增信息，不改 schema）

## Technical Approach

1. **分类 helper**（Python）：在 `llm.py` 复用已有 `_is_transient_api_error`，新增 `_classify_llm_error(e) -> {kind: "transient"|"permanent"|"unknown", retry_count, summary}`。
2. **Worker 包装** `worker.py:767`：`summary` 改为结构化友好串，如 `LLM 瞬时错误（已重试 5 次）: 503 system is busy`；同时设 `SubtaskResult.error` 字段。
3. **UI 友好渲染**：在 `status-formatter.ts` / `task-result-renderer.ts` / `subtask-tree-overlay.ts` 识别错误串里的瞬时/永久标记，加图标/颜色区分；`progress-widget.ts` 失败时显示首条错误摘要。
4. **不动** Rust/proto schema（错误仍走 summary/result 字符串字段，只是内容更友好）。

## Out of Scope

- 改 Rust proto schema 加 error 字段（保持字符串透传）。
- 主会话 Claude Code 客户端的 503 展示（不在本仓库）。
- 讯飞 MaaS 网关侧的 503 根因（网关容量问题，不可代码修复）。

## Technical Notes

- 关键文件：llm.py:595-683、worker.py:725-771、nats_worker.py:1493-1514、status-formatter.ts:77-79、progress-widget.ts:106-108、subtask-tree-overlay.ts:108-110、task-result-renderer.ts:58-60。
- 复用 PR #199 的 `_TRANSIENT_RETRY_MARKERS`。
- ponytail：错误分类是字符串匹配，不引入 LLM 调用判错。
