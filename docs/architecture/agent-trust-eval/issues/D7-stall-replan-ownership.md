# D7-stall-replan-ownership

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier; P2)_
Blocks: P2 replan implementation tickets

## Question

任务停滞时，系统应失败关闭、升级人工，还是**自动换一个新 Agent / 新分解重派**？谁拥有 replan 原语（Gateway / Orchestrator / Planner LLM）？与已有 `contract_changed`「由编排层重新规划」如何统一？

## Context

- 文档与 `progress.py`：无 replan 原语；停滞 → 停止重试。
- `contract.py` 已期望编排层在契约变更时重新规划，但实现侧仍缺原语。
- 用户 P2：「任务停滞时，换一个新的 Agent 重派一次。」

## Options

1. **单次自动重派**：同一 contract 下换 worker/agent 再跑一轮；仍失败则人工。
2. **真 replan**：新分解 / 新子图；需新 contract 版本与配额。
3. **仅人工**：Progress stop 后只发信号，不自动重派。

## Proposed (from intake — needs human confirm)

➡️ **选项 1 作为 P2 MVP**，选项 2 单列后续地图：先满足「换 Agent 重派一次」且可观测；真 replan 等 D3 完成条件与契约版本语义稳定后再做。

## Resolution

(fill in when decided)
