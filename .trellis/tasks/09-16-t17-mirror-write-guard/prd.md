# T17: 图镜像写入语义 —— seed-only + 转移守卫

- **Issue**: #667
- **Map**: P2 wayfinder map #656
- **Decision**: D15 #665 —— **已裁（2026-09-16）：B + C**
- **Priority**: P2
- **Status**: in_progress

## Goal

把 D15 的裁决落成代码：**图镜像永不覆盖 `state`（B）**，并对**已存在**的 node 加**转移守卫（C）**，使迟到的 legacy 快照无法回退图平面已裁决的状态（#664 的原始场景）。

## 背景（一句话）

`GraphStore::write_projection` 的 shadow 分支原先写：

```sql
ON CONFLICT (node_id, graph_id) DO UPDATE SET state = EXCLUDED.state, ...
```

`state` 是**无条件覆盖** —— 既无迁移门、也无版本 CAS。而镜像的来源是 legacy `TaskStore` 的 **fire-and-forget** fan-out，快照在调用瞬间捕获 ⇒ 一个迟到的陈旧快照可以覆盖更新的权威裁决。#664 已在 CI 上把这一形状稳定复现（`cancel_running_attempt` 写下 `READY` 后，更早捕获的 `InProgress` 快照把它盖回 `RUNNING`）。

## Scope

1. `write_projection` 的 shadow 分支：**`state` 从 `DO UPDATE SET` 移除**（B）。
2. **一次** SELECT 预取该 graph 的 `(node_id, state)`，避免逐节点查询。
3. 新增纯函数 **`mirror_write_allowed(prev_state, mirror_state)`**：
   `prev == incoming || transition_ok(prev, incoming)` —— 复用既有 `transition_ok`，**不新写第二份状态机**。
4. `BackfillStats` 新增两个计数：`mirror_state_dropped`（B 生效：state 与权威不符、故未被采纳）与 `mirror_rejected`（C 生效：整个写入被拒）。
5. `shadow = false`（import，source-A / source-B）路径**逐字不动**。

## Acceptance（对应 #667 票面）

1. **陈旧快照不改变权威 state**：#664 的原始形状（权威 `READY`、快照 `RUNNING`）⇒ `graph_nodes.state` 保持 `READY`，该 node 行**整体未被写入**，返回值里计为 `mirror_rejected == 1`。
   - ⚠️ **本节原文写的是「计为 `mirror_state_dropped`」，2026-09-16 已更正为 `mirror_rejected`。** 原因：`READY → RUNNING` **不是**合法边（派发走 `READY → SCHEDULED → RUNNING`），所以该形状是被 **C 整体拒掉**，不是被 B 落下状态 —— 见下条 B 的正主形状。
2. **B 的正主形状（Clause B 独立可观测）**：权威 `RUNNING`、快照 `READY` ⇒ `RUNNING → READY` **是**合法边（fence re-arm），C 放行，此时**只有 B** 能拦住回退 ⇒ `graph_nodes.state` 保持 `RUNNING`，非状态列（`dependencies`）照常刷新，计为 `mirror_state_dropped == 1`。
   - 两个计数器**互斥**（拒绝分支 `continue`，走不到 dropped 分支）⇒ `rejected + dropped` 恰为「快照与权威不一致的 node 数」。
3. **非法转移被整体拒绝**（权威 `SUCCEEDED`、快照 `RUNNING`）⇒ 写入跳过 + `mirror_rejected += 1`，**该 node 行一行未动**（`state` 与 `dependencies` 均保持原值；attempts/completions 是 append-only 路径，不属本守卫管辖）。
4. **常规镜像不被误拒**：快照与权威状态相同（只是来补 `dependencies`）⇒ 正常落库，两个计数都为 0。
5. **import 路径行为逐字不变**（`shadow = false`）。
6. 门禁全绿，Rust 基线只增不减。

### 消融证据（2026-09-16 实测，非推断）

用「一次只删一条子句、重跑 T17 集成测试」验证两条子句各自值多少钱：

| 注入的突变 | 结果 |
|---|---|
| 把 `state = EXCLUDED.state` 加回 `DO UPDATE SET`（**删掉 B**） | 测试在**形状 3** 失败：`left: Some("READY") right: Some("RUNNING")` —— 权威被迟到的 `READY` 快照回退了。形状 1/2 仍绿（C 拒掉了它们）。 |
| 把 `mirror_write_allowed` 恒返回 `true`（**删掉 C**） | 测试在**形状 1 的计数断言**失败：`nodes: 1, mirror_state_dropped: 1, mirror_rejected: 0`（期望 `nodes: 0 / rejected: 1`）——**但更靠前的 `node_state` 断言通过了** ⇒ 权威 state 没动。 |

**测量得到的结论（比原表更准，故据此改口径）：**

- **B 才是 state 的保证**：B 在，任何镜像写入都动不了 `state`，无论守卫怎么判。
- **C 是「一致性 + 可观测性」守卫**：它拦掉**已知不自洽**的快照（使其连非状态列都刷不进去），并把这件事变成计数而不是静默的部分写入。
- ⇒ 之前「形状 1 靠 C 拦住回退、形状 3 靠 B 拦住回退」的说法**过度声称**：形状 1 的 state 其实是 B 兜住的；C 在那个形状上的贡献是**拒绝整次写入 + 可计数**。
- ⇒ 「只留 C」会在形状 3 上破防（合法边 + 陈旧快照）；「只留 B」则让陈旧快照仍能静默改写结构且不可见。**两条都必须有**，这正是 D15 裁 B+C 的准确含义。

## Out of scope

- #664 已完成测试侧确定性修复（不动）。
- 「节点级三列是否改为各步之和」→ 归 #666 的实现票（T18）。
- 改 `UC_GRAPH_SHADOW` 默认值 —— **不改**：守卫落地后无需靠运维关开关；关掉它会让「图平面与镜像不一致」重新变成静默状态。

## 与既有铁律的关系（必须写清，否则守卫本身会变成新的违规）

铁律「图 node state 只能由**一份**依赖感知规则产出」约束的是 **`dependency_aware_state`**（一个 node **应当**是什么态）。本票需要的是**转移合法性**判断 —— 那是**另一份**规则，`transition_ok`（`graph_store.rs:227`），已被 `commit_once` / `cancel` / `claim` / `schedule_attempt` 共用。**复用它是把镜像路径接入同一份规则，不是制造第二份。**
