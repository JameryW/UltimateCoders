# D5-failure-type-reviewers

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier; parallel to P0)_
Blocks: P1 reviewer implementation tickets; may inform review-node graph shape

## Question

是否为**每种失败类型**单独一个评审器，并用**交叉验证**选阈值（禁止默认 0.5）？失败类型 taxonomy 是否必须对齐现有 `routing.py`？没有标注数据时默认行为是什么？

## Context

- `review.py` 仅 JSON 格式门（35 行）。
- `routing.py` 已有 `classify_failure` → 模型路由，但是路由不是评审阈值。
- 用户 P1 + 风险：每种评审器需要人工标注。

## Options

1. **对齐 routing taxonomy**：一类失败一个 reviewer + CV 阈值；无标注则该类型保持「人工 / 格式-only」且开关关闭。
2. **新 taxonomy**：与 routing 解耦，评审器按产品故障模式重划。
3. **推迟结构化评审**：先只加强 verify_command / D2 独立重跑；LLM review 仍格式门。

## Proposed (from intake — needs human confirm)

➡️ **选项 1**：与 `routing.py` 对齐以降低概念分裂；**无标注 ⇒ 该类型评审器不得启用**（fail closed 到现有格式门或人工）。阈值禁止提交默认 0.5 作为「已校准」。

## Resolution

(fill in when decided)
