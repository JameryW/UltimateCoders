# D3-completion-conditions-home

Status: **decided** (2026-10-24 — intake round-1 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: P0 tickets extending task/subtask contracts; must not fork a second “done” authority

## Question

结构化「完成条件」应住在哪里，并由谁判定为完成：扩展今日的 `contract.py`（今日只有 digest 版本）、子任务/图节点字段，还是仅留在 `verify_command` / review 节点？模型是否允许自报完成？

## Context

- `contract.py`：契约文本摘要变化 → `contract_changed`；无 completion 字段。
- 已有散点：`review.py` JSON `approved`、`step_condition`、`verify_command`。
- 用户 P0：「在 `contract.py` 里加上完成条件等结构化字段，由模型之外的代码判定。」

## Resolution

**双命名空间，从 `contract` 包导出。**

1. **`contract_digest` / `ContractRegistry`** 继续只回答「目标变了没」（F6）。
2. 新增 **`completion_conditions`**（结构化、可机器检查）与 digest **分结构**；API 从 `ultimate_coders.agent.contract`（或同包模块）导出，满足「在 contract.py / contract 包落地」的产品表述。
3. **完成判定必须非 LLM**：worker/orchestrator/runner 求值；模型自报 `done` / `approved` 不能单独过关。
4. 与 D2 组合：至少支持一类完成条件 =「在 Agent 不可写环境独立重跑通过」。
5. 现有 `verify_command` / `step_condition` / review 节点应收敛为条件类型或适配器，**不**另立第二套「完成权威」。
