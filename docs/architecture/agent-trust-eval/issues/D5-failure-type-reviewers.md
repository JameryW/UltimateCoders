# D5-failure-type-reviewers

Status: **decided** (2026-10-24 — round-2 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: P1 reviewer implementation tickets; may inform review-node graph shape

## Question

是否为**每种失败类型**单独一个评审器，并用**交叉验证**选阈值（禁止默认 0.5）？失败类型 taxonomy 是否必须对齐现有 `routing.py`？没有标注数据时默认行为是什么？

## Context

- `review.py` 仅 JSON 格式门（35 行）。
- `routing.py` 已有 `classify_failure` → 模型路由，但是路由不是评审阈值。
- 用户 P1 + 风险：每种评审器需要人工标注。

## Resolution

**对齐 routing taxonomy；无标注则该类型关闭。**

1. 失败类型 taxonomy **对齐** `routing.py`，降低概念分裂。
2. **一类失败一个 reviewer**；阈值**仅**经交叉验证选出。
3. **无标注数据 ⇒ 该类型评审器不得启用**（fail closed 到现有格式门或人工）。
4. 禁止把默认 0.5 作为「已校准」阈值提交进仓。
