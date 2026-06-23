# 文件冲突检测 — 调度时约束检查

## Goal

将已有的 ConflictDetector 接入 subtask 调度流程，在分配 subtask 前检查文件冲突，避免多 Worker 同时修改同一文件导致数据丢失。

## What I already know

- Rust `ConflictDetector` 已实现: declare_intent, check_conflict, remove_intent, baseline_hash
- Python `ConflictDetector` 已实现: overlaps, ConflictInfo, ResolutionTier
- Orchestrator 已有 `check_edit_conflict()` 和 `resolve_conflict()` 方法但从未被调度代码调用
- Subtask 有 `file_constraints` 字段但始终为空，从未填充或检查
- `_dispatch_remote()` 和 `_run_one()` 分配 subtask 时不检查文件冲突

## Requirements

### R1: LLM 分解结果填充 file_constraints

- Python `Orchestrator._decompose_task_with_llm()`: LLM prompt 增加 "List files that each subtask will modify" 指令
- `parse_llm_subtasks()`: 解析每个 subtask 的 `files` 字段，填充到 `file_constraints`
- 如果 LLM 不返回 files 字段，file_constraints 保持空（不阻塞调度）

### R2: 调度时冲突检查

- `_execute_subtasks()` 分配 subtask 前：检查 file_constraints 之间的重叠
- 同文件、不同 subtask → 标记 `PotentialConflict`，仍可分配但加日志
- 同文件、同区域 → 标记 `Conflicting`，延迟该 subtask 分配直到冲突 subtask 完成
- 无 file_constraints 的 subtask 不受约束（保守策略）

### R3: intent 生命周期管理

- subtask 开始执行时 `declare_intent(worker_id, files, regions=[])`
- subtask 完成/失败时 `remove_intent(file_path, worker_id)`
- task 完成时 `clear_intents()`

## Acceptance Criteria

- [ ] LLM 分解结果的 subtask 包含 file_constraints（当 LLM 返回时）
- [ ] 两个 subtask 的 file_constraints 有重叠时，日志警告
- [ ] 同文件区域冲突的 subtask 延迟分配（等待前一个完成）
- [ ] declare_intent/remove_intent 在 subtask 生命周期中正确调用
- [ ] 无 file_constraints 的 subtask 正常调度（无回归）
- [ ] 现有测试通过

## Definition of Done

- Tests added for conflict-aware scheduling
- Lint / typecheck green
- 不改 proto，不改 Rust 端

## Out of Scope

- 自动冲突解决（已有 resolve_conflict 方法，但自动 merge 逻辑不在本次范围）
- 行级区域检测（当前 regions 始终为空，whole-file 级别足够）
- Dashboard 冲突可视化
- NATS 广播冲突事件

## Technical Notes

- 关键文件: `python/ultimate_coders/nats_worker.py` (_execute_subtasks), `python/ultimate_coders/agent/orchestrator.py` (select_next_subtask, check_edit_conflict), `python/ultimate_coders/agent/conflict.py`
- ConflictDetector 已在 Orchestrator 初始化，直接调用即可
- 最小改动：在 `select_next_subtask` 或 `_execute_subtasks` 中加 conflict check 过滤
