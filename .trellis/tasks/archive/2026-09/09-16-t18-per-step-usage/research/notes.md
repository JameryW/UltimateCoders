# T18 侦察笔记（2026-09-16）

## 完整链路（逐步用量从哪来、到哪去）

```
adapter stdout
  └─ sandbox.py:1500  token_usage = usage        # 仅当流里有 usage 事件；否则保持 None
  └─ AgentOutput.token_usage                      # sandbox.py:345
       │
       ├─ [workflow] worker.py::_execute_steps    # 逐步跑，每步一个 AgentOutput
       │     └─ step_outputs: list[AgentOutput]   # 已执行步的输出（跳过的步不进）
       │     └─ 4 条 return：1366 / 1468 / 1482 / 1493 —— 每条只透传一步
       │
       └─ [legacy] worker.py:1237  sandbox_manager.execute(...)   # 单 agent，一次执行
              │
              └─ _execute_in_sandbox → SubtaskResult(usage=subtask_usage_from_token_usage(...))   # 1270
                   │
                   └─ nats_worker.py:2950 _make_subtask_result_task(usage=result.usage)
                        │
                        └─ nats_worker.py:161 _make_task_update_payload → entry["usage"]
                             │  ↑ 唯一集结口（T15/T16 都挂在这里）
                             └─ uc.task.update (NATS)
                                  │
                                  └─ uc-grpc/server.rs:139 NatsSubtaskUpdate.usage
                                       └─ :1609 PendingGraphVerb::Commit(result_ref, usage)
                                            └─ :935 fanout_graph_commit(...)
                                                 └─ engine GraphShadowSink::on_commit
                                                      └─ graph_store.rs:2977 → :1735 commit_once
                                                           └─ :1876 append_event_with_usage_tx("node_succeeded", payload, usage)
                                                                └─ execution_events(payload, cost, tokens, duration_ms)
```

**T18 就是在这一条链上把 `steps[]` 并行地搭上去**，每个环节都只加一个可选参数/字段。

## 改动清单（按文件）

### Python

| 文件 | 改动 |
|---|---|
| `agent/types.py` | 新 `StepUsage` dataclass（`step_index` / `parallel_group` / `usage` / `source`）+ `to_dict()`；`SubtaskResult.step_usages: list[StepUsage]` |
| `agent/sandbox.py` | `AgentOutput.step_usages: list[StepUsage]`（默认空列表 —— 与 `file_changes` 已累积的既有先例同型） |
| `agent/worker.py` | `_execute_steps` 在**两个 append 点**收集 `(idx, group, output)` → 4 条 return 各带上；`_execute_in_sandbox` 的 legacy 分支造一条隐式条目；`SubtaskResult(step_usages=...)` |
| `nats_worker.py` | `_make_task_update_payload` 的集结口：`if st.result.step_usages: entry["steps"] = [...]` |

### Rust

| 文件 | 改动 |
|---|---|
| `uc-types/src/agent.rs` | 新 `StepUsage` 结构（`step_index: u32` / `parallel_group: String` / `usage: Option<SubtaskUsage>` / `source: Option<String>`），**不加** `skip_serializing_if`（显式 null 是 D3 的要求） |
| `uc-types/src/lib.rs` | 根 `pub use` 加 `StepUsage`（**铁律**：漏了会 E0422/E0425） |
| `uc-engine/src/graph_store.rs` | `GraphShadowSink::on_commit` 加 `steps` 形参（默认实现同步改）；storage impl 透传；`commit_once` 加形参并把它写进 `node_succeeded` 的 payload；新增纯函数拆解器（供单测） |
| `uc-grpc/src/server.rs` | `NatsSubtaskUpdate.steps`；`PendingGraphVerb::Commit` 加第三项；`fanout_graph_commit` 加形参；29 处测试字面量补 `steps: None,`；测试 fake 的 `on_commit` 同步签名 |

## 关键形状（写代码时照抄，别临场发明）

```json
{
  "usage_reported": true,
  "usage_source": "grok-build",
  "steps": [
    {"step_index": 0, "parallel_group": "", "usage": {"input_tokens": 10, "output_tokens": 4}, "source": "grok-build"},
    {"step_index": 1, "parallel_group": "", "usage": null, "source": "codex"},
    {"step_index": 2, "parallel_group": "cr", "usage": {"input_tokens": 7}, "source": "claude-code"}
  ]
}
```

- `step_index` **跳号** = 那一步被跳过（条件为假）；**不是**丢数据。
- `usage: null` = 该步跑了、但适配器没报数字。
- 并行组的 N 个成员 = N 条，**无组级合计**。

## 门禁（本仓权威判据）

- Rust：`cargo check --workspace --all-features --all-targets`（**必须**——`storage`/`messaging` 门控会让
  测试文件编译成空文件，默认特征下**对签名变更完全瞎**；T15 就漏了 5 处 `commit_once` 调用）。
- 本机构建一律 `-j 1`（并行撞 `STATUS_STACK_BUFFER_OVERRUN` / `LNK1102`；崩溃留下的半写产物报
  `failed to mmap rmeta` / `can't find crate`，**重跑即自愈**，别 `cargo clean`）。
- Python：`.venv/Scripts/python.exe -m pytest <file> -o addopts=""`，**按文件串行**。
- 改动 `packages/**` 才触发 TS CI；本票不动 TS。
