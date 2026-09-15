# T15 侦察笔记 —— usage 从适配器到 `execution_events`

## 1. 端到端数据流（实测，逐跳）

```
[采集]  Python sandbox.py::TokenUsage{input_tokens, output_tokens, total_cost_usd}
        ├─ _grok_usage(value)                      sandbox.py:1244
        ├─ claude stream-json "result" 事件        sandbox.py:1586
        └─ 通用 JSON "usage" 字段                  sandbox.py:1656
          ↓ 装进 AgentOutput.token_usage           sandbox.py:345
[执行]  Worker._execute_in_sandbox → SubtaskResult  ✗ 在这里丢掉（worker.py:1239 不传）
[上报]  nats_worker.py:_execute_subtask_with_context → result   (2790/2794)
        → _make_subtask_result_task(task_id, subtask_id, status, summary, retry)
                                                  ✗ 结构上无 usage 位（nats_worker.py:2920+）
        → _make_task_update_payload(task, partial=True)   ← ★唯一收口点（nats_worker.py:118）
        → publisher.publish_update → NATS subject uc.task.update
[入口]  uc-grpc server.rs::NatsSubtaskUpdate（wire 结构，字段见 prd 偏差 1）
        ✗ 无 usage 字段
[落地]  server.rs 逐 subtask 循环 → subtask.result = Some(SubtaskResult{...})   (~1609)
        → graph_fanout.push((node_id, attempt, PendingGraphVerb::Commit(result_ref)))
[写点]  fanout_graph_commit → sink.on_commit(&env, result_ref.as_deref())        (server.rs:926)
        → GraphShadowSink::on_commit 默认 no-op                                  (graph_store.rs:599)
        → GraphStore::on_commit → commit_once(..., result_ref)                    (graph_store.rs:2804/2820)
        → commit_once 赢家分支 → append_event_with_usage_tx                       (T14 已就位)
        → execution_events.{cost,tokens,duration_ms}                              ✓ T14 绑了 duration_ms
```

**Rust 侧对照路径（本地执行）**：不存在。见 prd 偏差 2 的四条实证。

## 2. 单一收口点（两侧各一）

| 侧 | 收口点 | 说明 |
|---|---|---|
| Python | `nats_worker.py::_make_task_update_payload`（:118） | 所有 `uc.task.update` 上报都经它；逐 subtask entry 就在 `for st in task.subtasks` 里组装 ⇒ 加 `entry["usage"]` 覆盖**全部**调用点（:336 / :1852 / :2872） |
| Rust | `server.rs` 逐 subtask 循环 → `PendingGraphVerb::Commit`（:1544 / :1570） | `commit_once` 的生产调用点仍然唯一 |

## 3. 需要改的签名（编译器可驱动）

| 位置 | 现状 | 目标 |
|---|---|---|
| `uc_types::SubtaskResult` | 7 个字段，无 usage | + `usage: Option<SubtaskUsage>` |
| `NatsSubtaskUpdate` | `result: Option<String>` | + `usage: Option<SubtaskUsage>` |
| `GraphShadowSink::on_commit` | `(&self, &ExecutionEnvelope, Option<&str>)` | + `Option<&SubtaskUsage>`（默认实现保持 no-op） |
| `GraphStore::on_commit` | 同上 | 透传 |
| `GraphStore::commit_once` | `(gid, nid, attempt, result_ref)` | + `Option<&SubtaskUsage>` |
| `PendingGraphVerb::Commit` | `Commit(Option<String>)` | `Commit(Option<String>, Option<SubtaskUsage>)` |
| `fanout_graph_commit` | `(gid, nid, attempt, result_ref)` | + usage |

已知需要同步更新的实现/替身（`grep` 实测）：`server.rs:8529` 有一个测试 `GraphShadowSink` 实现；
`graph_store.rs:3084` 与 `graph_store_integration.rs:1600/1620` 是测试调用点。

## 4. `tokens` 单列 vs `input/output` 两值

`execution_events.tokens` 是 **BIGINT 单列**，而 usage 有 input/output 两个数。约定：

- 至少一侧非空 ⇒ `tokens = input.unwrap_or(0) + output.unwrap_or(0)`（真实的和）；
- **两侧都缺 ⇒ NULL**（不是 0）；
- `total_cost_usd` 缺失 ⇒ `cost` NULL。

⇒ `SubtaskUsage` 上提供 `total_tokens() -> Option<i64>`，与 T14 的 `attempt_duration_ms` 同款
"诚实函数"：**能推出真值就给真值，推不出就 `None`**。

## 5. 类型层注意事项（承 T14 的坑）

- `cost` 列是 `NUMERIC(18,6)`，sqlx 的 `f64` 是 `FLOAT8` ⇒ 绑定必须写成
  **`$7::float8::numeric`**（T14 已落地，本票沿用，不改）。
- `tokens` 是 BIGINT ↔ Rust `i64`：`Option<i64>` 直接绑，无需 cast。
- payload 里 `usage_reported` 是**布尔**，`usage_source` 是字符串（缺则整个键不写）。

## 6. 已知限制（交付时必须写进票面评论）

1. **Rust 本地执行路径不存在**（prd 偏差 2）⇒ 本票只交付转换 helper + 单测，
   **不声称"本地执行路径已落列"**。
2. **PG 集成测试本地跑不了**（沙箱把 `wsl.exe` 拉黑，见 memory 与 skill §5.6）⇒ 新加的
   PG 断言**编译验证 + 交 CI 的 `storage integration tests` job**，并明确写"未在真实 PG 上执行"。
3. ~~`source` 只能在"适配器静态可知"的解析函数里填（`_grok_usage` → `grok`，claude stream-json →
   `claude_code`）；通用 JSON 解析器路径没有 adapter 身份 ⇒ `source` 缺省为空，
   payload 里就不写 `usage_source`。**不编造来源。**~~
   **→ 见 §7.2：这条判断被推翻**（三个解析点都在适配器方法内，`self.name()` 可用）。

## 7. 实现补记（交付时回填：两处票面未覆盖 + 一条原判断被推翻）

1. **checkpoint 这一跳也会丢**（已补）：`Task.to_dict`/`from_dict` 是 checkpoint 往返，
   原先不含 usage ⇒ worker 重启后恢复、再以 `partial=False` 重发全量快照
   （`nats_worker.py:336`/`:1852`）时 usage 静默消失。补 `to_dict` 的 `"usage"`
   （`usage.to_dict() if usage else None`）与 `from_dict` 的 `SubtaskUsage.from_dict`；
   无 key ⇒ `None`（加性）。与 D13 的"某一跳丢掉"是同一类缺陷。
2. **§6.3 的悲观判断被推翻**（已改）：三个 usage 解析点都在适配器方法内部
   （`_grok_usage` 的调用点、claude 的 stream-json 与 legacy 两处），`self.name()` 在作用域里
   ⇒ `source` **三处全部填上**（`grok-build` / `claude-code`），不存在"通用解析器无身份"这条路径。
   `_grok_usage` 因此多一个 `source` 形参（模块级函数看不到 `self`）；
   `subtask_usage_from_token_usage(usage, source=None)` 的显式参数可覆盖它。
3. **多步 workflow 只报最后一步**（已知限制，本票不修）：`_execute_steps` 每条返回路径都只透传
   一步的 `token_usage`（正常路径 `last_output`，两条 abort 路径取失败步）。
   **少报不是错报**；聚合会让数字与 `source` 同时不可归因，逐步用量应落在逐步事件上。
4. **worker→worker 的远程结果**事件**不带 usage**（已知限制）：`subtask_completed` 事件
   （`nats_worker.py:2866` 发送、`:3125` 接收）只带 `summary`/`modified_files`/`error`。
   网关落列不受影响（每个 worker 各自发 `uc.task.update`，那是本票接的口），但**本地编排器里
   那份远程子任务副本的 usage 是空的** —— 若它随后被当作全量快照重发，那一次上报就没有 usage。
5. **运维命令**：`cargo fmt` 会重排 `impl.rs`/`lib.rs` 的 re-export 列表（小写名字排前），
   以及修正脚本插入字段时按"闭合括号缩进"算出的少 4 空格缩进 —— 交付前**必须**跑一次
   `cargo fmt --all`，`cargo check` 抓不到缩进。

