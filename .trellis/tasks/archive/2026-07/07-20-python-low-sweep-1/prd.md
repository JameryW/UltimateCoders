# PRD: Python LOW Sweep 1（#12/#13/#14）

## 背景

/loop 第 25 轮。Python 审计剩 LOW 项，取三项：#12（错 id 日志 + raise 无失败结果）、#13（importance 0.0 强转 + search None guard）、#14（/metrics 绕鉴权——安全项优先）。

## 清单（已核实）

### F60: _execute_subtasks gather 异常处理（审计 #12，LOW-MED）

`results` 对应 `local_batch`，异常日志却用 `batch_ids[i]`（含 remote 派发 id）→ remote 批非空时**记错 subtask id**（误导事故分诊）。且 `_run_one` raise 仅记日志——subtask 留在 RUNNING/ASSIGNED，任务停滞无失败上报。

修：`sid = local_batch[i]` 记日志；raise 时 `handle_subtask_result(SubtaskResult(subtask_id=sid, worker_id=本 worker, summary="Execution raised: ..."[:200], success=False))`——任务可终态收敛。

### F61: memory importance/search 健壮性（审计 #13，LOW）

- `MemoryEntry.from_rust`（memory.py:97）`float(getattr(raw, "importance", 0.5) or 0.5)`——合法 `importance=0.0` 被 `or` 当 falsy 强转 0.5（影响长期记忆晋升阈值 ≥0.7 的判定边界……实际 0.0→0.5 改变晋升行为）。修：`imp = getattr(raw, "importance", None); float(0.5 if imp is None else imp)`。
- `LongTermMemory.search`（memory.py:368-377）直接迭代 `raw_results`——engine 返回 None 时 TypeError。修：`for raw in (raw_results or [])`。

### F62: /metrics 端点鉴权（审计 #14，LOW，安全）

dashboard 其余路由皆有 `_check_auth`，唯 `/metrics`（app.py:724）无——网络暴露时 task/worker/error metrics + worker id 对未认证客户端泄露（与声明的鉴权模型矛盾）。修：同款 `_check_auth` 门。

## 验收

- test_memory_entry.py：importance=0.0 round-trip 保持 0.0（非 0.5）；importance 缺失 → 0.5 默认。
- 新/扩展：LongTermMemory.search engine 返回 None → []（不抛）。
- dashboard 测试：DASHBOARD_PASSWORD 设置时 /metrics 未认证 401。
- pytest tests/python 全绿 + ruff exit 0；feature branch + PR + CI green。

## 不做（下轮）

#8 SSE fan-out（MED，独立设计）；#9 file broadcast 大小上限；#11 JetStream replay 死码；#15 PTY 线程泄漏/SearchQuery 校验。
