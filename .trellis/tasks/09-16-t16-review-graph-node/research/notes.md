# T16 侦察记录（recon）

所有结论均**实测**，非推断。命令与证据随条附。

## §1 对 D14 的三处事实更正

### 1.1 `SubtaskResult.review`：只在 TS 侧存在

| 侧 | 定义位置 | 有 `review`？ |
|---|---|---|
| Rust `SubtaskResult` | `crates/uc-types/src/agent.rs:210-232` | ❌ 无 |
| Python `SubtaskResult` | `python/ultimate_coders/agent/types.py:153-174` | ❌ 无 |
| TS `SubtaskDef` | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:106-110` | ✅ 有 |

TS 侧字段注释原文（`orchestrator.ts:106-110`）：

> Supervisor review verdict — T6 #642 C7 retired the TS review pipeline (nothing
> produces this anymore); the field stays for the UI to render review data from
> older caches and **for the future Rust-side pipeline to repopulate**.

⇒ D14「`SubtaskResult.review` 被刻意保留」**只对 TS 成立**。UI 能渲染靠的是 TS 字段，
但 **Rust→TS 根本没有这条通道**（见 §1.4）。

### 1.2 `graph_nodes.type`：有列、有 DEFAULT、**但无写者**

- 列定义（`graph_store.rs:751`）：`type TEXT NOT NULL DEFAULT 'subtask'` ✅ D14 正确；
- 唯一 INSERT 站点（`graph_store.rs:1030` / `1038`，shadow / non-shadow 两变体）列清单：
  `(graph_id, node_id, state, dependencies, required_capabilities, effect_class)`
  —— **不含 `type`** ⇒ 所有行恒为 `'subtask'`。

⇒ "零迁移"成立，**"零工作"不成立**。本票必须提供写者（S1）。

### 1.3 `worker_epoch` 不是 worker 身份

- `graph_store.rs:1411`：`SELECT COALESCE(MAX(retry_no),-1)+1, COALESCE(MAX(worker_epoch),0)+1`
  ⇒ 是 **per-attempt 单调栅栏**（fence token），不是 worker 的身份/世代；
- `nats_worker.py:111`：Python 侧恒发 `"worker_epoch": ""`。

⇒ 票面「同一 worker epoch 不得自审」按字面不可实现（该值在 Python 侧恒空）。
**按语义实现为「产出被审结果的那次 attempt 的 worker 不得认领该 review 节点」**。

### 1.4 Rust→TS 没有 review 通道

- `crates/uc-grpc/proto/engine.proto`：`grep -n review` **零命中** ⇒ `SubtaskProto` 无 review 字段；
- `grpc-bridge.ts`：`grep -n review` 只命中 `reviewing` 状态映射（:215），
  `parseTaskFromProto`（:1036-1060）映射了 `result/retryCount/requiredCapabilities/steps`，
  **没有 `review`**。

⇒ 验收「结论经 `SubtaskResult.review` 送达」需要**新增 proto 字段 + 桥映射**（S4）。
这不是"改 UI"（UI 读的是 TS `SubtaskDef.review`，已存在）。

## §2 唯一收口点

| 关注点 | 收口点 | 证据 |
|---|---|---|
| 图节点写入 | `graph_store.rs:1030/1038`（两条 INSERT，同一函数） | `grep -n "INSERT INTO graph_nodes"` 只有 2 处 |
| `NodeRow` 构造 | `graph_store.rs:356`（Rust Task）、`:482`（TS PersistedTask） | `grep -n "NodeRow {"` 只有 2 处 |
| 能力门 | `worker_service.rs:262 dispatch_gate(&required, &project_id)` | 调用点 `server.rs:2790` / `:3845` |
| Readiness | `dependency_aware_state`（T13 #655） | `graph_store.rs:473` |

## §3 独立性：**能力 + affinity 表达不了「排除」**（关键发现）

| 机制 | 能否表达"排除产出方" | 证据 |
|---|---|---|
| 能力门 | ❌ | `dispatch_gate` 只按 **capability** 和 **scope** 过滤，无排除参数（`worker_service.rs:262-290`） |
| affinity | ❌ | `placement.rs:7` 原文：**"preference, never a gate"**；排序 `affinity desc → load asc → locality desc → worker_id asc`，无 anti-affinity |
| `worker_epoch` | ❌ | 是 fence token（§1.3），与 worker 身份无关 |

**雪上加霜**：`worker.py:399` 把 `"review"` 放进了**每个** worker 的默认能力清单
（`caps = ["code","search","memory","test","decompose","review"]`）⇒
产出方 worker **自带** review 能力，能力门对它完全放行。

### 结论与裁法

D14 假设"独立性由派发约束保证"。实测**派发侧没有排除原语**，加一个就等于改派发路径
（验收明令禁止，且要求"回来重开 D14 而不是就地扩权"）。

⇒ **本票按 D14 的备选口径实现**（D14 原文：「若你接受自评…第 3 条可以撤」的反向——
即用**能力门**而非派发代码）：把 `"review"` 从默认能力清单摘掉，
使 **review 节点只能被显式配置为 reviewer 的 worker 认领**。产出方 worker 默认没有该能力
⇒ 通不过 `dispatch_gate` ⇒ 节点保持 PENDING 而不被自派。

**残留（诚实记录）**：若某个 worker **同时**被配置成执行者与 reviewer，它仍能自审。
彻底堵死需要 `dispatch_gate` 支持排除集 ⇒ **那属于 D14 的模型洞，按验收应回 D14，
不在本票就地扩权。** 见 §5 已知限制。

## §4 worker 侧已有的 review 资产（不要重复造）

`python/ultimate_coders/agent/worker.py`：

- `:399` 默认能力清单含 `"review"`（**本票要摘掉**）；
- `:479-484` `AGENT_PROFILES["review"]`：`disallowed_tools: ["Edit","Write","NotebookEdit"]`
  + `append_system_prompt: "Read-only review mode…"` ⇒ **只读 reviewer 画像已存在**；
- `:537-542` `SUBTASK_TEMPLATES["review"]`：同上；
- `:601-603` `_template_for`：描述含 `review|audit|analyze|inspect` 自动套 review 画像。

⇒ S3/S4 只需**接线**，不需要重新设计 reviewer 的行为约束。

## §5 已知限制（交付前必读）

1. **自审禁止是"能力门级"而非"派发级"**：默认配置下产出方通不过 review 能力门；
   显式双身份 worker 仍可自审。堵死需改 `dispatch_gate`（回 D14）。
2. **`type` 仍只写不读**：本票只补写者，不新增任何按 `type` 分支的行为（D14 决定 5）。
3. **review 结论的语义由 worker 侧产出**：本票只做通路，不做"审什么/几轮/失败重试"。

## §6 本轮交付范围（2026-09-16）

**已交付并验证**：S1（图平面 `type` 写者）、S2（能力路由，无需改代码）、S3（独立性）。

**未交付**：S4（review 结论经 `SubtaskResult.review` 送达 TS）。原因如实记录：
S4 横跨 uc-types / `NatsSubtaskUpdate` wire / proto / conversions / Python 构造 /
checkpoint 往返 / TS grpc-bridge **六个落点、四种语言面**，且本机**没有 protoc**
（`which protoc` 为空）⇒ uc-grpc 与 `--all-features` 的编译在本机本就不可跑，
改 proto 后无法在本地完成闭环验证。**宁可留白也不提交验证不了的跨语言契约改动。**
⇒ #661 保持 open，`task.json` 保持 in_progress，下一轮续做 S4。

## §7 门禁归因：一条失败命令的定性

`cargo clippy -p uc-engine --all-targets -- -D warnings` 失败
（`graph_store_integration` / `storage_integration` 报 `can't find crate`）。
**没有当成"改坏了"也没有当成"环境问题就算了"，而是把改动 `git checkout` 回干净基线再跑同一条命令** ——
基线上**同形失败**（`use uc_engine::GraphStore; can't find crate`）⇒ 与本票无关。

佐证两条：① `which protoc` 为空 ⇒ proto 相关生成代码不可得；② 同批次还出现过
`rustc` 以 `STATUS_STACK_BUFFER_OVERRUN` (0xc0000409) 崩溃，属编译器/资源层面故障。
真正能验本票改动的门禁（`clippy -p uc-engine --lib`、`clippy -p uc-types --all-targets`、
`check -p uc-engine --all-targets`、`test -p uc-engine --lib`）**全绿**。
