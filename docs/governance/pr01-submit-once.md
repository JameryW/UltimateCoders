# PR01 — Submit-Once：一个提交最多创建一个逻辑任务

代码：`crates/uc-types/src/governance/submission.rs`（类型与纯函数）、
`crates/uc-engine/src/governance/submission.rs`（提交记录存储）、
`crates/uc-engine/src/governance/outbox.rs`（事务外发箱）。
传输契约定在 `crates/uc-grpc/proto/engine.proto:273-300`（`SubmitTaskRequest`/`SubmitTaskResponse`）。

## 1. 要解决的问题

原提交路径是「在 store 建 task → 发 NATS → 发布失败就删占位」。三种时序会出错
（模块文档原话，`crates/uc-types/src/governance/submission.rs:1-20`）：

- 建完 task、未发出就崩溃 → 提交丢失，客户端只能重试；
- 发出去了、没等到 ack 就崩溃 → 客户端重试后出现**两个** task；
- 客户端只是网络超时重试 → 同样出现两个 task。

所以 PR01 要三件事同时成立：**最多一个逻辑 task**、**不丢提交**、**不静默分叉**。

## 2. 协议

```
client ──(submission_id 或派生)──► SubmissionStore::submit_once
                                     │  ① create-only CAS 写 submission 记录
                                     │  ② 幂等 enqueue outbox 条目
                                     ▼
                              OutboxQueue ──► NATS `uc.task.submit`（at-least-once）
                                     │                     │
                                     ▼                     ▼
                           mark_delivered           消费端按 (submission_id, task_id) 去重
                                     │                     │
                                     └──── mark_applied ◄───┘（消费端确认已建编排）
```

顺序是硬约束：**先写记录，再写外发箱**。反过来的话，可能出现「已投递但没有记录」的窗口，
那正是重复建 task 的来源（`crates/uc-engine/src/governance/submission.rs:84-93` 的注释）。

> 发布端只有一条路径：`crates/uc-grpc/src/governance/bridge.rs:30-110` 的 `drain_outbox`
> （feature `messaging`）。它先 `claim_due`（→ `in_flight`）、再 publish、再 `mark_delivered` 或
> `schedule_retry`；单趟上限 `OUTBOX_DRAIN_LIMIT = 64`，避免大积压堵住提交路径。定时驱动这一循环属于
> 网关装配，不在本模块内。

## 3. 身份推导

| 量 | 规则 | 出处 |
| --- | --- | --- |
| `submission_id`（调用方给） | 原样 trim 保存；空值由调用方拒绝（空键会把无关提交合并） | `submission.rs:61-66` |
| `submission_id`（派生） | `auto-` + `sha256(principal|project_id|request_digest)[..32]` | `submission.rs:68-76` |
| `request_digest` | `sha256(project_id \0 normalize_request(description))` | `submission.rs:109-112` |
| `normalize_request` | CRLF→LF、逐行去尾空白、去首尾空行；**措辞差异（哪怕一个否定词）必须改变摘要** | `submission.rs:93-107` |
| `task_id` | `sub-` + `sha256("task:" + submission_id)[..32]` | `submission.rs:127-131` |

派生 id 的意义在于：即使「记录丢失但已投递」这种最坏情况发生，消费端算出的 task id 仍然相同，
不会出现第二个逻辑任务。测试：
`derived_task_id_is_stable_across_retries`、`auto_submission_id_is_deterministic_for_same_request`、
`request_digest_ignores_whitespace_noise_but_not_wording`（同名于 `crates/uc-types/src/governance/submission.rs` 测试模块）。

## 4. 状态机

### 4.1 提交记录（`SubmissionState`，`submission.rs:133-178`）

```
            ┌──────────┐   transport 接手   ┌───────────┐  消费端确认   ┌─────────┐
  submit ──►│ received │ ─────────────────► │ published │ ───────────► │ applied │
            └────┬─────┘                    └─────┬─────┘              └─────────┘
                 │                                │
                 │ 校验失败/策略拒绝/重试耗尽        │ 运维放弃
                 ▼                                ▼
            ┌────────┐                      ┌───────────┐
            │ failed │（终态）               │ abandoned │（终态）
            └────────┘                      └───────────┘
```

`is_terminal()` = `applied | failed | abandoned`（`submission.rs:172-177`）。
`failed` 明确「永不静默重试」——重试耗尽必须被人看见。

**谁把 `received` 推到 `published`**（2026-11-05 修正）：外发箱派发路径（`drain_outbox`）在
`mark_delivered` 成功后就地确认提交记录（`confirm_published_submission` → `mark_published`），
用 `outbox_key` 的反解 `submission_id_from_outbox_key` 只认 `task-submit:` 前缀，别的平面共用外发箱
命名空间也不会被误改。此前只有 gRPC 提交路径（`server.rs`）会声明 `published`，而 NATS 桥的
`submission.submit` 是唯一真实入口，于是 `submission.get` 永远停在 `received`、`publish_attempts` 恒为 0，
本地实测（提交后 4 秒查）坐实了这一点。测试：
`a_delivered_publication_advances_the_submission_record`（gRPC 边界）、
`a_crash_between_publish_and_the_record_update_is_confirmed_not_republished`（引擎层）。

`applied` 由消费端**显式回执**：桥上是 `submission.apply`（见 §6），由 orchestrator 在任务创建成功后调用。

### 4.2 外发箱条目（`OutboxState`，`submission.rs:332-356`）

```
        claim_due                 mark_delivered
pending ──────────► in_flight ──────────────────► delivered
   ▲                    │
   │  boot: recover()   │ schedule_retry ×12
   └────────────────────┴──────────────────────► dead（等运维）
```

- 认领即 `in_flight`：崩溃在这里**只能靠重投恢复**，所以是 at-least-once 而非 at-most-once
  （`submission.rs:338-341`）。
- 退避：`250ms × 2^attempts`，上限 30s；`OUTBOX_MAX_ATTEMPTS = 12`
  （`submission.rs:50-52`、`submission.rs:408-424`）。
- 重新入队同 key 是 no-op，所以恢复扫描不会复活已 delivered 的条目
  （`crates/uc-engine/src/governance/outbox.rs:1-16`；测试 `enqueue_is_idempotent_by_key`、`delivered_entries_are_never_reclaimed`）。

## 5. 失败矩阵

| 崩溃/异常点 | 持久状态 | 恢复动作 | 结果 | 依据 |
| --- | --- | --- | --- | --- |
| 写记录**之前** | 无任何记录 | 客户端重试同一 `submission_id` | 新建一次，不重复 | `submit_once` 的 `Created` 分支（`crates/uc-engine/src/governance/submission.rs:102-106`） |
| 写完记录、未写 outbox | `received`，无 outbox 条目 | 启动时 `recover_unfinished` 读到 `received`，用**同一个** task id 补排队 | 不丢，不重复 | `submission.rs:248-300`；测试 `crash_between_record_and_outbox_is_recovered_with_the_same_task_id` |
| 已写 outbox、投递中被杀 | 条目 `in_flight` | `OutboxQueue::recover()` 把 `in_flight` 置回 `pending` 并立即到期 | 可能重复投递（设计上接受） | `outbox.rs:141-160`；测试 `in_flight_entry_is_recovered_after_crash`、`recover_requeues_entries_orphaned_in_flight` |
| 已投递、未 mark_delivered | 记录 `received`/`published`；条目 `delivered` | 记录被重投一次；消费端幂等吸收 | 至多一次重复投递 | 上两行合并；`recover_unfinished` 只处理 `received`/`published` |
| 已 mark_delivered、记录仍 `received` | 条目 `delivered`，记录 `received` | 恢复扫描发现已投递就**确认**成 `published`（`confirmed`），不再重投 | 不丢、不重投；记录追上事实 | `recover_unfinished` 的 `delivered` 分支；测试 `a_crash_between_publish_and_the_record_update_is_confirmed_not_republished`；本机实测日志 `requeued=0 confirmed=1` |
| 重复投递（同 key 两条） | outbox key 相同 → 第二次 enqueue no-op | 无 | 不产生第二个 task | `outbox_key()` = `task-submit:<submission_id>`（`submission.rs:308-311`） |
| 复用 `submission_id` 但内容不同 | 既有记录摘要 ≠ 请求摘要 | 返回 `Conflict{submission_id, existing_digest, requested_digest}`，**不创建任何东西** | 显式冲突，不静默重放 | `crates/uc-engine/src/governance/submission.rs:117-124`；测试 `same_key_different_content_is_rejected_as_conflict`、`same_id_different_content_is_a_conflict_shape` |
| 投递连续失败 12 次 | 条目 `dead` | `dead_letters(limit)` 供运维取出 | 停在 `dead`，不无限转 | `outbox.rs:162-170`；测试 `failing_publishes_back_off_then_dead_letter` |

## 6. 载荷与消费端幂等边界

outbox payload（`crates/uc-engine/src/governance/submission.rs:142-150`）：

```json
{
  "task_id": "sub-…",
  "description": "…",
  "project_id": "…",
  "submission_id": "…",
  "case_id": "…（可空，PR02 链接）"
}
```

恢复路径的 payload 额外带 `"recovered": true`（`submission.rs:270-276`），让消费端/日志能区分
「首次投递」和「恢复重投」。

**消费端必须按 `(submission_id, task_id)` 去重**——这是 at-least-once 的另一半
（`crates/uc-engine/src/governance/outbox.rs` 注释明确写了「This can produce a duplicate delivery, which is why the
consumer dedupes」）。执行面目前能提供的最强保证是：`task_id` 是派生值，`tasks` 表以 `id` 为主键，
`TaskStore::submit_task` 是纯 `INSERT`（无 `ON CONFLICT`，`crates/uc-engine/src/task_store.rs:308-325`），
因此重复投递**不会**产生第二行，但会以存储错误的形式返回。

**消费端回执已经落地（2026-11-05）**：桥上的 `submission.apply` 接受 `submission_id`（必填）与
`task_id`（可选，但必须等于派生 id，否则 `task_id_mismatch`），调用者需要 `task_submit`，重复确认幂等；
Python 侧由 orchestrator 在 `submit_task` 成功后回写（`NatsWorker._confirm_submission_applied`，
best-effort：任务已经存在，回执失败只丢审计状态，不抛错）。dev 快照给 `agent:uc-worker` 授
`task_submit`，生产应把同一权限授给真正的消费方身份。测试：
`the_consumer_can_confirm_a_submission_as_applied`（桥）、
`test_the_consumer_confirms_applied_through_the_bridge`（Python 线形状）。

仍未实现的是原始注释里的另一半：**把「主键冲突」翻译成 applied**。重复投递时 orchestrator 在本地
去重处直接返回，不会走到回执；因为 `task_id` 是派生值且 `tasks` 以主键唯一，这在正确性上无害
（不会多一份工作），只是 `applied` 需要一次成功提交才会被记录。

`mark_applied` 的存在意味着状态推进是**显式**的（`crates/uc-engine/src/governance/submission.rs:191-202`）；
没有回执时记录停在 `published`，这比假装 `applied` 更诚实。

## 7. 验收：怎么手工验

1. **类型层**：`cargo test -p uc-types governance::submission`
   （命中 `derived_task_id_is_stable_across_retries`、`request_digest_ignores_whitespace_noise_but_not_wording`、
   `same_id_different_content_is_a_conflict_shape`、`outbox_retry_backs_off_and_dies_after_the_cap`、
   `in_flight_entry_is_recovered_after_crash`）。
2. **存储层**：`cargo test -p uc-engine governance::submission`
   （`first_submit_creates_exactly_one_task_and_one_outbox_entry`、
   `retry_returns_the_same_task_and_does_not_duplicate_the_outbox_entry`、
   `crash_between_record_and_outbox_is_recovered_with_the_same_task_id`、
   `mark_published_and_applied_advance_the_state_machine`、
   `derived_task_id_is_the_single_source_of_identity`）。
3. **协议层**（gRPC 可用后）：用同一个 `submission_id` 连发两次 `SubmitTask`，第二次必须满足
   `dedup_replayed = true` 且 `task_id` 相同（字段定义见 `engine.proto:288-300`）；换一份
   `description` 再用同一 id，必须失败而不是新建。
4. **崩溃演练**（进程级）：在 outbox 条目处于 `in_flight` 时杀掉网关，重启后确认该条目回到
   `pending` 并被重投一次——对应 `outbox.rs:141-160`；重复投递是**预期**结果，不是 bug。
5. **消费端回执**：以带 `task_submit` 的 principal 向桥发 `submission.apply`（`submission_id` +
   可选 `task_id`），记录应从 `published` 变成 `applied`；把 `task_id` 换成别的值必须返回
   `task_id_mismatch`；重复调用保持 `applied`。

## 8. 未实现 / 有意留空

| 项 | 现状 | 原因 |
| --- | --- | --- |
| 消费端按 `submission_id` 去重 | 未实现（见 §6） | 属执行面 `uc.task.submit` 消费者改造，与本层的身份设计解耦 |
| 后台 dispatcher 的**定时驱动** | 发布动作本身已落地（`crates/uc-grpc/src/governance/bridge.rs` 的 `drain_outbox`）；「多久跑一趟」的主循环不在本模块 | 调度频率属部署参数；分开的好处是原语可在测试里确定性驱动，也能在网关启动时按需触发 |
| 一次性「记录 + 外发箱」事务 | 内存/文档存储下是两次 CAS；PostgreSQL 下注释说明可落在同一事务（`crates/uc-types/src/governance/submission.rs:22-30`），但代码路径仍走两次写 + 恢复补偿 | 两写 + 恢复已被测试证明等价；引入跨表事务会绑定单一后端 |
| `submission_id` 的空值校验位置 | 在调用方（`SubmissionId::caller_supplied` 只 trim，不报错） | `submit_once` 无法区分「空键」和「幂等键确实为空」的语义，交给 API 层判断 |
