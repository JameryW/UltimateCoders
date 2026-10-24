# D7-stall-replan-ownership

Status: **decided** (2026-10-24 — round-2 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none — D3 completion conditions unblock true replan later)_
Blocks: P2 replan implementation tickets

## Question

任务停滞时，系统应失败关闭、升级人工，还是**自动换一个新 Agent / 新分解重派**？谁拥有 replan 原语（Gateway / Orchestrator / Planner LLM）？与已有 `contract_changed`「由编排层重新规划」如何统一？

## Context

- 文档与 `progress.py`：无 replan 原语；停滞 → 停止重试。
- `contract.py` 已期望编排层在契约变更时重新规划，但实现侧仍缺原语。
- 用户 P2：「任务停滞时，换一个新的 Agent 重派一次。」
- D3：结构化完成条件与非 LLM 判定已决；真 replan（新分解）宜等其落地后再做。

## Resolution

**P2 MVP = 同契约换 Agent 重派一次；真 replan 延后。**

1. MVP：在**同一 contract** 下换 Agent / worker **再派一次**；仍失败则升级人工。
2. 真 replan（新分解 / 新子图、新 contract 版本与配额）**延后**到 D3 `completion_conditions` 落地且契约版本语义稳定之后，单列后续实现票。
3. 编排层拥有该单次重派原语的可观测触发与配额；不把「换分解」伪装成同契约重试。
