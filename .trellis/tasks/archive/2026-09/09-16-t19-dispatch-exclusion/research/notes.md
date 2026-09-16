# T19 #670 — 侦察笔记

> 全部结论都带一手来源（文件或命令输出）。凡「某某已存在/已通过」都要有一条可复现的依据。

## 1. 现有的 review 标签是怎么来的（T16 留下的）

- `crates/uc-engine/src/graph_store.rs:333` `pub const NODE_TYPE_REVIEW: &str = "review";`
- `graph_store.rs:341` `pub const REVIEW_CAPABILITY: &str = "review";`
- `graph_store.rs:352` `pub fn node_type_for(required_capabilities: &[String]) -> &'static str`
- `graph_store.rs:381` `pub fn requires_independence(required_capabilities: &[String]) -> bool`（本票新增；`node_type_for` 改为调用它）

**关键结论**：`review` 这个标签是**派生**的（由能力集算出），不是声明出来的 ⇒ 本票的判定必须**复用**同一个谓词，否则就会制造「标签说它是 review、派发按普通节点走」两份规则。

## 2. roster 是不是单一来源？（决定排除落在哪）

`crates/uc-grpc/src/worker_service.rs`：

| 行 | 函数 | 说明 |
|---|---|---|
| 272 | `workers_with_capabilities_excluding` | **唯一**算能力 roster 的地方（本票新增） |
| 296 | `workers_with_capabilities` | 无排除特例，**仅供能力查询**（文档明说不是派发口径） |
| 302 | `dispatchable_workers_with_capabilities` | 测试专用，从 296 派生 |
| 319 | `dispatch_gate` | 派发口 1 |
| 408 | `dispatch_candidates` | 派发口 2（affinity 打分要拿到候选人本身） |
| 440 | `placement_target` | 从 `dispatch_candidates` 派生 |

⇒ 排除落在这**一处**，则门、候选、打分三者**同时**看到同一份排除，T12 那条不变式（*scoring must never see a worker the gate would reject*，就写在 `placement_target` 的文档注释里）**自动成立**，不需要在三个地方各写一遍。

## 3. 生产者身份从哪来

- `crates/uc-types/src/agent.rs:109` `pub assigned_worker: Option<WorkerId>`
- `crates/uc-grpc/src/server.rs:1669`
  ```rust
  if let Some(worker) = &subtask_update.assigned_worker {
      subtask.assigned_worker = Some(uc_types::WorkerId(worker.clone()));
  }
  ```
  **只赋值、从不清空** —— 这是「retry 黑窗」在本路径上碰不到的直接依据。
- 成功上报会带 `assigned_worker`（legacy `TaskStore` 发的 `NatsSubtaskUpdate` 是稀疏形状：`subtask_id`/`status`/`assigned_worker`，所以这一条**正是**重新落定身份的那一步）。
- review 节点只在其依赖 `SUCCEEDED` 后才可能 `READY` ⇒ 上面这条覆盖了所有真正会走到的路径。

## 4. 派发实际怎么送达（决定本票能改变什么、不能改变什么）

- `server.rs:467` `fn resolve_dispatch_subject(...)` —— placement 返回 `None` 时**回落**到共享 subject。
- 共享 subject 是 durable work-queue（`subtask-workers`）；`placement_target` 返回 `None` 是**正常**结果（affinity 是**软偏好**，T12/D12 明确）。
- legacy worker **没有** per-worker subject（`python/.../nats_worker.py:538-540`）。

⇒ **本票能做的**：在「候选人只有生产者」这个**最坏形状**上拒绝派发（静默自审 → 可见 `PENDING`）。
⇒ **本票做不到的**：候选人 ≥2（含生产者）时的竞态窗口仍然存在 —— work-queue 仍可能投给生产者。要消除它需要 per-worker subject 全覆盖 + affinity 升格为门，与 D12 冲突，**已记入 out of scope 并如实写进 prd**。

## 5. 别把间接信号当事实：两次读代码纠正了读来的印象

1. **`orchestrator.ts:233` 一度被我读成「覆盖写」**：`def.requiredCapabilities = [...new Set(merged)]` 看起来会把手写的 `["review"]` 冲掉。实际上一行 `merged = [...explicit, ...stepAgents]` 说明它是 **union**。⇒ 「手写能力被冲掉」**不成立**。
2. **副产品（有用的一条）**：既然是 union，那么规范 grok→codex 链里那个 review 步的 `agent` 是**空串**，所以**不会**意外把节点升格 —— 但「某个步的 `agent` 字面量恰好是 `"review"`」这种共享命名空间冲突**是真实的**，本票只钉住语义（验收 6）。
3. **票面上关于 `assigned_worker` 被清空的那句话**（D16 立案时我写的）被 §3 的代码推翻 ⇒ 已写进 prd 的「一次自我更正」。

## 6. 测试放在哪（因为签名是必填形参，纯逻辑可脱离 store）

- `review_independence` 与 `dispatch_gate` 的检查都**不碰 IO** ⇒ 用 `Subtask`/`Task` 字面量直接构造即可，**4/5 条验收不需要 live PG**。这与 T18 把 `steps_payload` 抽成未门控纯函数的动机相同：**形状契约要能在本地被钉住**。
- 需要**忠实重建**整条 task 的快照时（`server.rs` 侧两条），走既有模式：
  `serde_json::json!({...})` → `NatsTaskSnapshotResponse` → `apply_task_snapshot_response(&mut store, resp)`（返回计数），而不是手搓 `TaskStore` 内部字段。
- `TaskStore::get_task` 返回 **`Option<&Task>`**（借用），而 `uc_engine::TaskStore::get_task` 返回 owned ⇒ 循环里还要 `&mut store` 改 status，因此必须取 owned 快照（`.cloned()`），否则借用冲突编不过。

## 7. 枚举的 serde 表示（写 fixture 前先确认，别猜）

- `SubtaskStatus` / `TaskStatus` **没有** `rename_all` ⇒ JSON 里是 **PascalCase**：`"Pending"` / `"Completed"` / `"InProgress"` / `"Assigned"`。

## 8. 本机环境（本轮实测）

- `cargo fmt --all -- --check` clean。
- `cargo clippy --workspace --all-targets --all-features -j 1 -- -D warnings` clean；`cargo clippy --workspace -j 1 -- -D warnings`（CI 原样命令）clean。
- ⚠️ **`-j 1` 必须写在 `--` 之前**：写成 `cargo clippy --workspace -- -D warnings -j 1` 时 `-j` 被送给编译器，报 `error: Unrecognized option: 'j'`，而 `tail` 截断后**看起来**像已知的 `.rmeta` 损坏症状（"could not compile uc-types"）。差一点按「重跑即自愈」处理，抓完整输出才看到真因。
- ⚠️ 本轮两次撞到 `rustc ... STATUS_STACK_BUFFER_OVERRUN (0xc0000409)`（`uc-grpc --all-features --lib` 与 `uc-engine --all-features --lib`）：**原样重跑即自愈**，两次都跑出预期值（249 / 468）⇒ 是产物损坏的**症状**，不是环境缺陷，别 `cargo clean`。
- **本机 live PG 本轮不可用**：`timeout 5 bash -c '</dev/tcp/127.0.0.1/5432'` → `Connection refused`。T19 不含任何 `#[ignore]` PG 测试 ⇒ 不影响本票；公开签名变更由 `cargo check --workspace --all-targets --all-features` 覆盖（clean）。

## 9. 🔴 一次「间接信号当事实」的险情：spec 与代码不一致，代码才是事实

读 `.trellis/spec/backend/agent-capability-spec.md` 想确认「worker 的能力集里有没有 `review`」时，该文件 §`_derive_capabilities` 写着：

```
Base: ["code", "search", "memory", "test", "decompose", "review"]
```

若照此推理，就会得出「**每个** worker 默认都持有 `review` ⇒ 只要部署里只有一个 worker，T19 的硬门就会让所有 review 节点**永久卡在 PENDING**」——这是一条会写进票面与记忆、且看起来很有说服力的**错误**结论。

去读一手代码（`python/ultimate_coders/agent/worker.py:433-448`）后发现它**恰好相反**：

```python
caps = ["code", "search", "memory", "test", "decompose"]
```

并且 T16 自己留了注释解释为什么：*"T16 (#661): 「review」is deliberately NOT a default capability."* 真正的开关是环境变量 **`UC_CAP_REVIEW`**（`worker.py:495`，模式与 `UC_CAP_BROWSER`/`UC_CAP_DEBUG` 相同）。

**由此得到本票正确的定位（与险些写下的那句不同）**：

1. 默认 worker **根本不持有** `review` ⇒ 它对 review 节点先在能力门上就是 `NoCapableWorker`，**到不了** T19 的新检查。所以 T19 **不是**「新引入的常见路径阻塞」，而是**残余缺口的收口** —— 与 D16 的措辞（*显式双角色 worker 仍可自审*）严丝合缝。
2. 新的 fail-closed 路径**可被触达的前提**是「存在一个 opt-in 了 `UC_CAP_REVIEW` 的 worker」。此时：若它是唯一候选人 ⇒ `NoIndependentReviewer`；若依赖的生产者身份未知 ⇒ `ProducerIdentityUnknown`。两者都不再静默自审。
3. T16 的注释里还有一句 *"The dispatch side has no exclusion primitive"* —— **T19 之后这句话变成假的** ⇒ 已同票更正该注释（否则它就是一个指向不存在事实的路标）。
4. 已同票修正 spec 里那行错误的能力集（`.trellis/spec/backend/agent-capability-spec.md`），并补上 `UC_CAP_REVIEW` 的说明。

**方法论**：这一条与 T16 那次「Bun 说 *not your code* 而实际是真语法错误」是**同一类**——**文档/规格是间接信号，代码是一手事实**。规格过期时它比没有规格更危险，因为它自带权威感。
