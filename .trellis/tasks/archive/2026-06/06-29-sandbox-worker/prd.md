# 完善 sandbox 模式下 worker 的能力

## Goal

增强 sandbox 模式下 worker 的能力，补齐重试、进度可见、agent 专业化三个关键短板。

## Requirements

1. **Subtask 自动重试**：失败 subtask 按策略自动重试（最多 3 次，退避 2s/4s），复用已有 `retry_count` 字段
2. **Worker 进度报告**：在 `execute_subtask` 关键阶段发出 `subtask_progress` 事件（preparing→executing→validating→finalizing），带 `phase` + `percent` 字段
3. **更多 agent profiles**：新增 test/fix/refactor/deploy/docs 五个 AGENT_PROFILES
4. **更多 subtask templates**：新增 test/fix/refactor/deploy/docs 五个 SUBTASK_TEMPLATES，扩展 `_match_subtask_template` 关键词匹配

## Acceptance Criteria

* [ ] 失败 subtask 可自动重试最多 3 次，退避 2s→4s，retry_count 正确递增
* [ ] Worker 执行过程中发出 subtask_progress 事件（phase + percent）
* [ ] AGENT_PROFILES 覆盖 ≥6 个场景（review/codegraph/code + test/fix/refactor/deploy/docs）
* [ ] SUBTASK_TEMPLATES 覆盖 ≥6 个场景，关键词匹配覆盖常见动词
* [ ] 现有测试通过，新增重试逻辑有测试覆盖

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* 无新外部依赖

## Technical Approach

### 重试
- 在 `execute_subtask` 中，失败后检查 `subtask.retry_count < MAX_RETRIES`，若可重试则 sleep 退避后重新执行
- 每次重试递增 `subtask.retry_count`，发出 `subtask_retry` 事件
- SubtaskResult.retry_count 已有字段，直接复用

### 进度
- 在 `execute_subtask` 的关键点插入 `_publish_event("subtask_progress", ...)` 调用
- 阶段：preparing(10%) → executing(50%) → validating(80%) → finalizing(95%)
- 不改 streaming callback，那是逐行 stdout，进度是阶段级

### Profiles/Templates
- 扩展 Worker.AGENT_PROFILES 和 Worker.SUBTASK_TEMPLATES 类属性
- 扩展 `_match_subtask_template` 的关键词映射

## Decision (ADR-lite)

**Context**: Worker 缺少重试、进度、专业化 agent 配置，导致分布式执行可靠性差、可观测性低
**Decision**: 有限重试(3次+退避) + 阶段级进度事件 + 扩展 profiles/templates
**Consequences**: 重试增加总执行时间但提高成功率；进度事件增加事件量但量小；profiles 硬编码在类属性中，后续可外部化

## Out of Scope

* Docker sandbox 隔离增强
* LLM-based agent 配置推导
* 新的 gRPC proto 定义
* output 质量验证（expected_output 检查）— 留后续
* 从配置文件加载 profiles — 硬编码够用

## Technical Notes

* 关键文件：python/ultimate_coders/agent/worker.py, python/ultimate_coders/agent/types.py
* AGENT_PROFILES: worker.py:252-266
* SUBTASK_TEMPLATES: worker.py:268-279
* _resolve_agent_config: worker.py:281-309
* _match_subtask_template: worker.py:312-322
* execute_subtask: worker.py:368-528
* SubtaskResult.retry_count: types.py:77
