# T15 (#660) —— usage 上报契约：`SubtaskResult` 加 usage + `cost`/`tokens` 落列

- **地图**：P2 wayfinder map #656
- **决策**：D13 #657（已裁）
- **上游**：T14 #659（写入点 + `duration_ms`，已交付）
- **分支**：`main`（本仓直落 main，不建 PR）

## 背景

D13 把"三列由谁写"裁定了：**网关在 `commit_once` 的同一事务内写**，`duration_ms` 由网关从
`task_attempts` 时间戳推导，`cost`/`tokens` **经结果上报契约以加性可选字段携带**，缺失一律留
NULL **不得当 0**。

T14 已交付第一片（`duration_ms` 落在 commit-once 赢家分支，恰好一次是结构性的）。
本票交付第二片：**让适配器早已采集到的 token/cost 真正到达那两列**。

## 侦察纠正的三处票面偏差（T14 之后的实况，已实证）

票面（与 D13）写于 T14 之前，三处与今天的代码不符。**逐条列出，不静默扩张也不静默遗漏**：

### 1. 真正的上线报文不是 `SubtaskResult`，而是 `NatsSubtaskUpdate`

票面 Scope 1 说"`SubtaskResult` 加可选 usage 字段"（Rust `uc-types/src/agent.rs:210-222`）。
但 `SubtaskResult` 在 Rust 侧是**网关自己构造的 domain 类型**（`server.rs:1611`、
`nats_subtask_to_domain`），它**不从 wire 反序列化**。worker 上报的报文结构是：

```rust
// crates/uc-grpc/src/server.rs
pub struct NatsSubtaskUpdate {
    pub subtask_id: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub description: Option<String>,
    pub depends_on: Option<Vec<String>>,
    pub result: Option<String>,      // ← 只有 summary 字符串
    pub attempt_id: Option<u64>,
}
```

⇒ **usage 要在这一层加字段**，否则 worker 上报的用量到不了网关。`SubtaskResult`（domain）
同样加字段（D13 明确要求），但它是**承运方**，不是入口。

### 2. Scope 4「Rust 本地执行路径」在今天是**死路径**（有实证）

票面 Scope 4 要求"Rust 本地执行路径同样把 `AgentOutput.token_usage` 传下去"。实证核查：

| 事实 | 证据 |
|---|---|
| `AgentAdapter::parse_output` **无生产调用者** | 全仓 `parse_output` 命中只有 `impl` 与 `#[cfg(test)]` 单测（`grep -rn parse_output crates/`） |
| `create_adapter` / `available_agents` 同样只在测试里 | `agents/mod.rs:107-140` 全是单测；`lib.rs:61` 只是 re-export |
| `SandboxExecutor` 是**显式占位**，`available() == false` | `scheduler/executor.rs:253-270`，`execute` 直接返回 `Unsupported("lands with the sandbox runtime (post-T7)")` |
| `LocalNodeHandler` **无生产实现** | 只有 `RecordingHandler`（单测）与 `ToolRunnerHandler`（`tests/executor_nats_down.rs`） |

⇒ Rust 侧今天**没有任何**"执行 agent → 得到 `AgentOutput` → 走到 commit 写点"的路径；
`token_usage` 因此才"在 `crates/` 内无任何消费方"。**本票不臆造这条路径**：改为提供
`SubtaskUsage::from_token_usage(...)` 这样的**公共转换 helper + 单测**，让 sandbox runtime 落地时
接线是一行；并在票面评论里把"未实现生产者"这件事写成已知限制。

### 3. Python `SubtaskResult` 的 usage 在**上报途中被丢弃**（两处）

- `worker.py:1239` 的 `_execute_in_sandbox` 从 `AgentOutput` 构造 `SubtaskResult` 时**不传** `token_usage`（其余字段都传了）。
- `nats_worker.py:_make_subtask_result_task(task_id, subtask_id, status, summary, dispatch_retry_count)`
  只接 `summary`，**结构上没有位置放 usage**。

⇒ 补这两个点，加 `_make_task_update_payload` 里的 `entry["usage"]`。

## Scope（与票面对齐，含上述补齐）

1. **契约**：新增 `uc_types::SubtaskUsage`（`input_tokens` / `output_tokens` / `total_cost_usd` / `source`
   全为 `Option`，`serde(default)` + `skip_serializing_if`），并在 `SubtaskResult` 上加
   `usage: Option<SubtaskUsage>`（**加性 + 可选 ⇒ 不动 `contract_version`**）。
2. **wire**：`NatsSubtaskUpdate` 加 `usage: Option<SubtaskUsage>`（复用同一个类型，两侧字段名逐字一致）。
3. **Python 镜像**：`types.py` 加同构 `SubtaskUsage` dataclass + `SubtaskResult.usage`；
   `sandbox.py::TokenUsage` 加 `source`；上游两处丢弃点补齐。
4. **网关**：`PendingGraphVerb::Commit` 携带 usage → `fanout_graph_commit(..., usage)` →
   `GraphShadowSink::on_commit(env, result_ref, usage)` → `commit_once(..., usage)` →
   `append_event_with_usage_tx` 绑 `cost` / `tokens`；payload 写 `usage_reported: true` +
   `usage_source`。
5. **缺失语义（D13 硬要求）**：无 usage ⇒ 两列留 NULL + `usage_reported: false`；
   **任何路径都不得把缺失折算成 0**。`tokens` 单列由 input+output 求和，但**只有至少一侧非空**才算；
   两侧都缺 ⇒ NULL。
6. **Rust 本地/沙箱路径**：提供转换 helper（`TokenUsage` → `SubtaskUsage`）+ 单测；
   **不接生产者**（见偏差 2）。

## 验收（票面原文）

- 有 usage 的上报 → 两列非空、payload `usage_reported: true` 且带来源。
- **无 usage 的上报（普通 CLI adapter）→ 两列留 NULL、`usage_reported: false`；任何聚合都不得把 NULL 当 0**（D13 硬性要求）。
- 跨语言字段名一致（golden 测试，参照 `test_merge_gate.py` / `test_affinity_placement.py` 的既有做法）。
- 本地执行路径也落列。
- 门禁全绿且 Rust 基线只增不减。

> 验收第 4 条按偏差 2 的口径交付为"转换 helper 就位且被单测覆盖；生产者不存在（有实证）"，
> 已在票面评论里显式写明，**不声称"本地执行路径已落列"**。

## 非目标

- 指标本身（Useful Work Ratio / Coordination Ratio / Activation Inflation 的定义与计算）。
- Rust sandbox runtime 的接线（`SandboxExecutor` / `LocalNodeHandler` 的生产实现，post-T7）。
- worker 直写 PG（D13 已否决）。
- `SubtaskResult` 的既有字段（`review` 等）不动。

## 实现补记（交付时回填）

实现过程中出现**三处票面未覆盖 / 与侦察判断不符**的内容，逐条记录：

### 1. checkpoint 这一跳也会丢 usage（已补）

`Task.to_dict` / `from_dict` 是 **checkpoint 往返**（不是 NATS 报文，报文由
`_make_task_update_payload` 负责）。原先 subtask result 的字段全在往返里，usage 不在 ⇒
worker 重启后从 checkpoint 恢复、再以 `partial=False` 发全量快照（`nats_worker.py:336` / `:1852`）
时，同一子任务的 usage **静默消失**。

这正是 D13 那个"采到了却在某一跳丢掉"的**同一类缺陷**，故顺手补上：`to_dict` 写
`"usage": st.result.usage.to_dict() if st.result.usage else None`，`from_dict` 用
`SubtaskUsage.from_dict` 读回，**无 key ⇒ `None`**（加性，老 checkpoint 一律 `None`）。

### 2. `source` 三处解析点**全都能**拿到适配器身份（原侦察判断偏悲观，已推翻）

侦察 §6.3 预计"通用 JSON 解析路径没有 adapter 身份 ⇒ `source` 缺省为空"。实测：三个
usage 解析点**都在适配器方法内部**（`_grok_usage` 的调用点、claude 的 stream-json 与 legacy
两处），`self.name()` 就在作用域里 ⇒ 三处全部填上（`grok-build` / `claude-code`）。
`_grok_usage` 因此多一个 `source` 形参（它是模块级函数，看不到 `self`）。

**仍未编造来源**：`None` 进 ⇒ `None` 出；缺来源时 payload 不写 `usage_source`。

### 3. 多步 workflow 只上报最后一步的 usage（**已知限制，本票不修**）

`_execute_steps` 的每条返回路径都只透传**一步**的 `token_usage`（`worker.py` 正常路径取
`last_output`，两条 abort 路径取失败步）。三步 workflow 报一步的量，是**少报而非错报**。

**不在本票聚合**：一个 workflow 可串多个适配器，合成一个块会让数字与 `source` **同时不可
归因**；逐步用量应落在**逐步事件**上，那是另一个设计问题。已在 `_execute_steps` 的返回处留注释。

