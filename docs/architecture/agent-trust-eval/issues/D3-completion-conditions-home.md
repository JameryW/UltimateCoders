# D3-completion-conditions-home

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier)_
Blocks: P0 tickets extending task/subtask contracts; must not fork a second “done” authority

## Question

结构化「完成条件」应住在哪里，并由谁判定为完成：扩展今日的 `contract.py`（今日只有 digest 版本）、子任务/图节点字段，还是仅留在 `verify_command` / review 节点？模型是否允许自报完成？

## Context

- `contract.py`：契约文本摘要变化 → `contract_changed`；无 completion 字段。
- 已有散点：`review.py` JSON `approved`、`step_condition`、`verify_command`。
- 用户 P0：「在 `contract.py` 里加上完成条件等结构化字段，由模型之外的代码判定。」

## Options

1. **扩展 contract 模块**：同一 registry 增加 machine-checkable `completion_conditions[]`；worker/orchestrator 在模型外求值；模型自报无效。
2. **节点级 verify only**：完成条件只挂在 verify/review 图节点；contract 保持 digest-only，避免概念过载。
3. **双写过渡**：digest 仍管「目标变了没」；完成条件新 namespace，但 API 从 `contract` 包导出以匹配用户命名。

## Proposed (from intake — needs human confirm)

➡️ **选项 3**：满足「在 contract.py（包）暴露完成条件」的产品表述，同时不把 digest 身份与完成判定揉成一个模糊结构。判定代码必须非 LLM；与 D2 的独立重跑可组合为一条完成条件类型。

## Resolution

(fill in when decided)
