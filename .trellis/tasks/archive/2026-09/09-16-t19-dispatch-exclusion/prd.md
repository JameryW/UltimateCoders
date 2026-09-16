# T19 #670 — 派发硬门排除「产出者」：review 节点不得由自己审

**Status**: delivered
**Implements**: D16 #669 裁决 **A**
**Map**: #656（P2 地图）
**Related**: #661（T16，review 作为图上节点 — 本票补上它如实记账的独立性残余）、#654（T12，affinity placement — 本票落在它的共享 roster 上）、#649（D12）、#656 §5.18

---

## Goal

T16（#661）把 `review` 变成图上一个**有类型**的节点，并让**派发侧**（架构 2 节点级）真的按「谁产出、谁审」分派。但 T16 交付时如实记了一笔**残余**：那套独立性只靠**编排层自律**（分解器给 review 步换个 agent），**网关侧没有任何硬门**。于是「显式声明双角色的 worker」（同一个 worker 既产又审）可以**自审**，而且外观上完全正常 —— 审阅结论照样进 `SubtaskReview`，**下游没有任何东西能把它和真独立审阅区分开**。

本票落地 D16 裁决 A：把「排除产出者」做成**派发硬门**，落在**唯一一处** roster 计算点上，使「能力不足 / 没人可审 / 不知道谁产的」三种失败各自**可见、可区分、不静默**。



## 背景：为什么是「硬门」而不是「更聪明的路由」

`review` 节点的语义是**独立性**：结论的可信度来自「审的人不是写的人」。T16 之后这条语义的**唯一**保障是编排层的自觉 —— 一个显式声明了双角色（`["code","review"]`）的 worker 会让「写」与「审」落到同一个进程里，产出一个**在数据上无法与真独立审阅区分**的 verdict。这正是本仓反复付代价的那类失效形状：**失效外观与正常外观相同**（T12 的 affinity 静默失效、T17 的假 `RUNNING`）。

于是问题不是「怎么把 review 送到另一个 worker」，而是「**在没法保证独立时，宁可不动**」。硬门的成本是「没人可审时节点停在 `PENDING`」——**可见**；放行的成本是「结论看起来正常但不可信」——**不可见**。两者不对称，所以选硬门。

### 先说清本票**不是**什么：它不制造新的常见路径阻塞

一手事实（`worker.py:433-448`）：**`review` 不是默认能力**。默认能力集是 `["code","search","memory","test","decompose"]`，T16 刻意把 `review` 排除在外，opt-in 开关是环境变量 **`UC_CAP_REVIEW`**（`worker.py:495`）。

⇒ 一个**没有** opt-in 的 worker，对 review 节点在**能力门**上就已经是 `NoCapableWorker`，**根本走不到** T19 的新检查。所以：

- 单 worker 的默认部署**不会**因为本票而卡住 review 节点（它本来就走不到那里）；
- 新检查**可被触达的前提**是「集群里存在一个 opt-in 了 `UC_CAP_REVIEW` 的 worker」——此时**要么**它去审自己产出的东西（原缺口），**要么**停在 `PENDING`（本票）。

即：本票是**残余缺口的收口**（与 D16 的措辞「显式双角色 worker 仍可自审」一致），**不是**新增的常规阻塞。

> ⚠️ 这一条差点被写反：`.trellis/spec/backend/agent-capability-spec.md` 当时仍写着默认能力集**包含** `review`，照它推理会得出「单 worker 部署永久卡住」的耸动结论。去读一手代码才发现 spec 过期。已同票修正 spec，并把 T16 那句「派发侧没有排除原语」（本票之后变成假话）一并更正 —— 详见 `research/notes.md` §9。

## 侦察：决定设计的六条一手事实

| # | 事实 | 一手来源 |
|---|---|---|
| 1 | 派发是**共享 work-queue**（durable `subtask-workers`），`resolve_dispatch_subject` 在 placement 返回 `None` 时**回落**到共享 subject，而 `None` 是**正常**结果（affinity 是软偏好） | `server.rs::resolve_dispatch_subject` |
| 2 | `dispatch_gate` / `dispatch_candidates` / `placement_target` **三者同源**：后两者都从能力 roster 派生 ⇒ 排除落在 roster 上，则 T12 那条不变式（*"scoring must never see a worker the gate would reject"*）**自动成立** | `worker_service.rs:272/408/440` |
| 3 | 生产者的身份**就在快照里**：`Subtask::assigned_worker` | `uc-types/src/agent.rs:109` |
| 4 | 成功上报**会重新落定** `assigned_worker`，且该字段在更新路径上**只被赋值、从不被清空** | `server.rs:1669`（`if let Some(worker)`） |
| 5 | review 节点**只可能**在其依赖到达 `SUCCEEDED` 之后才被派发 ⇒ 第 4 条覆盖了真正会走到的那条路径 | `dependency_aware_state` 的依赖语义 |
| 6 | `required_capabilities` 由 TS 分解器按 **union** 派生（`[...explicit, ...stepAgents]`），而规范 grok→codex 链里那个 review 步的 `agent` 是**空串** ⇒ 不会意外把节点升格 | `orchestrator.ts:226-233` |

第 4 条是**读代码读出来的**，它当场推翻了我写在票面上的一句话（原话是「失败会清掉 `assigned_worker` ⇒ 回落逻辑必须一起裁」）—— 见「一次自我更正」。第 5 条则是它的**配套**：即使某些路径会暂时清空该字段，review 也到不了 `READY`。

## 设计：D16 裁决 A 的落地

**排除集作为 roster 的必需形参，不做可选包装。** `workers_with_capabilities_excluding(required, exclude)` 是**唯一**一处算 roster 的地方；`workers_with_capabilities`（无排除）保留给纯粹的能力查询，并**在文档里明说它不是派发口径**。两个派发口（`dispatch_gate` / `dispatch_candidates`）都把 `exclude` 作为**必填**参数 —— 若做成 `Option` 或带默认值，将来任何一个新的派发口都可以**少写一个参数就静默恢复自审**，而那正是本票要关掉的形状。

**两条 fail-closed 检查，顺序「越具体越先」，且计数器分开。**

| | 检查 | 含义 | 计数器 |
|---|---|---|---|
| 1 | `unknown_producers` 非空 | 依赖**跑了但没人记下是谁跑的**（PG backfill / `.uc/tasks` 导入 / 快照不全）⇒ 独立性**无法核实** | `producer_identity_unknown` |
| 2 | 排除后候选人**为空** | 生产者被识别了，而它**是唯一**持有该能力的人 ⇒ **没有独立审阅者** | `no_independent_reviewer` |

**两个计数器刻意不合并。** 「没人能审」（补一个 reviewer worker 即可）与「不知道谁产的」（补 worker 一点用都没有，要去修生产者的上报口径）是**两个不同的事实、两个不同的修法**。合并后节点停在 `PENDING` 而操作者**无从判断是哪一种** —— 这与 T12/T17 是同一种病。

**`NoCapableWorker` 的语义刻意不动**：它由**排除前**的 roster（`capable`）判定，所以「没人有这个能力」与「只有产出者有此能力」永远可分。

**加性保证（本票最重要的一条）**：`requires_independence == false` ⇒ `review_independence` 返回 `ReviewIndependence::default()`（两个字段都空）⇒ `dispatch_gate` 对非 review 节点的行为与改动前**逐字节相同**（第一个检查不可能触发，第二个检查的候选集等于原集合）。

## Scope

1. `uc-engine::graph_store` 新增**公开谓词** `requires_independence`，并让 `node_type_for` **调用它**（同一份规则，不是抄两遍）。
2. `WorkerRegistry` 新增 `workers_with_capabilities_excluding`，`workers_with_capabilities` 改为它的无排除特例。
3. `dispatch_gate` 增加 `independence` 形参 + 两条 fail-closed 检查 + 两个 `AtomicU64` 计数器与只读访问器。
4. `dispatch_candidates` / `placement_target` 增加 `exclude` 形参（排除在**共享的** roster 上，不在某一个派发口上）。
5. `WorkerDispatchGate` 新增两个变体，各带载荷：`ProducerIdentityUnknown { dependencies }`、`NoIndependentReviewer { producers }`（**排序后**返回，HashSet 的迭代序不是判定依据）。
6. `review_independence(subtask, Option<&Task>)` 自由函数 + `ReviewIndependence` 类型：从快照读出每个依赖的 `assigned_worker`。
7. 两个派发口（`server.rs` 两处近同构代码）接上：取**owned** 快照、算独立性、把 `exclude` 一路传到 placement。
8. 测试：5 条验收 + 消融自检。

## Acceptance

1. 双角色 worker 是**唯一**候选人时，硬门**拒绝**（`NoIndependentReviewer`，载荷=被排除的生产者），节点保持 `PENDING`；补进第二个 reviewer 后**同一节点**恢复可派发。
2. 存在独立 reviewer 时：门放行，且 `placement_target` **绝不**选中被排除的生产者（哪怕它 affinity 最高、负载最低）—— 无约束时会选中它，有约束时选中另一个 ⇒ **差值是排除本身造成的**。
3. 依赖的 `assigned_worker == None` ⇒ `PENDING` + `ProducerIdentityUnknown`（**不是** `NoIndependentReviewer`），且两个计数器**各记各的**。
4. **非 review 节点逐字节不变**（回归护栏，最重要的一条）：生产者是唯一能力持有者时，普通节点**照常派发**。
5. retry → success 的路径**重新落定**生产者身份（`server.rs:1669` 只赋值不清空）。
6. 共享命名空间语义**用测试钉住**：一个 `agent` 字面量为 `"review"` 的步会把节点升格（本票**不解决**它，只钉住语义，见 Out of scope）。
7. 门禁全绿、基线**只增不减**、`--all-features` 编译（公开签名变更）。

## Out of scope

- **共享命名空间冲突**：步的 `agent` 字面量为 `"review"` 时，节点会被升格成 review 节点（`required_capabilities` 是 union 的必然结果）。本票只**钉住**该语义（验收 6），**不**改分解器的命名/派生规则 —— 那要另开一张票，因为它牵动编排层契约而不只是 Rust 侧。
- **把 target 派发推广到 legacy worker**：裁决 D 已被否决，理由是**结构上不可能**而非代价高 —— legacy worker **没有** per-worker subject（`nats_worker.py:538-540`），共享 work-queue 是**默认**路径 ⇒ 定向投递覆盖不到它们；且那会把 affinity 从**偏好**升格为**门**，与 D12 冲突。
- **竞态窗口**（如实记账，**不假装已消除**）：候选 ≥2（含生产者）时，work-queue 仍**可能**把消息投给生产者。硬门能改变的只是**最坏形状**（候选人**只有**生产者时的静默自审），它**不能**把共享队列变成定向投递。要真正消除需要 per-worker subject 覆盖 + affinity 升格为门，即上一条。
- 节点级指标口径、`graph_nodes` 列形状、review 事件 payload 形状。

## 与既有裁决的关系

- **#666 / T18 的先例**：把「能被测试钉住的形状」抽成纯函数，让门控外的部分可测。本票的同型选择是把 `review_independence` 与两条检查做成**不依赖 store** 的纯逻辑 —— 于是 5 条验收里 4 条能在 `uc-grpc --lib` 里跑，不需要 live PG。
- **#654 / T12**：不新增第二条 roster 规则，而是**复用**那一处；T12 的不变式因此自动成立（事实 2）。
- **#650 / T8 的 scope 门**：新的两条检查**排在** scope/version 之前（越具体越先），因为 scope 判定会**点名候选 worker**，而那个名字会把操作者引向一个**不是问题**的 scoped worker。

## 一次自我更正（写在票面里，不留口头）

D16 立案时我写过「失败会清掉 `assigned_worker`，所以 `None` 的回落逻辑必须与本裁决一起定」。裁决阶段读代码后**推翻**了它：`apply_update` 里 `assigned_worker` **只被赋值、从不清空**（`server.rs:1669`），而 review 节点只在其依赖 `SUCCEEDED` 之后才可能 `READY` ⇒ 「retry 黑窗」在本路径上**碰不到**。真正会产生 `unknown_producers` 的是**从未上报过生产者**的图（PG backfill / 导入），那正是第二个计数器要量的东西。这个更正也解释了为什么两个计数器必须分开：**它们对应的现实场景不同**。
