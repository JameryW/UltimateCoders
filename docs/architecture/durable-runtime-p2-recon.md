# Durable Runtime P2 开票前置勘察（2026-09-18）

> 本文**不是** P2-1 / P2-2 / P2-3 的本体设计，也**不是**决议票。
> 它只做一件事：把**仓内已确证的事实**与**只有外部「方案第 21 节」原文能回答的问题**分开列清，
> 使原文到达后可以直接开票，不必再花一轮做侦察。
> 每条仓内事实都带 `file:line`（**2026-09-18 实测**）；没有原文的地方一律**留空位，不臆造**。

## 为什么现在只能做到这一步

评估件 §六（本日复核）：开放决策 **0**、待落地票 **0**；P2-1 / P2-2 / P2-3 的**本体仍无据**。
仓内实测：`PlacementScore` **0** 命中、`Execution Optimizer` **0** 命中、`market` 仅 **3 处无关命中**
（明细见 §三）⇒ 三个本体的**语义定义**在仓内无处可坐实。本文据此只列事实与空位。

## 一、P2-1 Execution Optimizer

### 已确证的前置（均已交付并关票）

| 项 | 票 | 一手锚点（2026-09-18） |
|---|---|---|
| 指标列预留 | T2（早于 P2） | `execution_events` 表 `cost NUMERIC(18,6)` / `tokens BIGINT` / `duration_ms BIGINT`（`crates/uc-engine/src/graph_store.rs:928-930`） |
| 写入点落地 | T14 #659（承 D13 #657） | 终态事件绑定 `duration_ms`：`EventUsage { duration_ms: attempt_duration_ms(attempt_started_at, db_now) }`（`graph_store.rs:1845-1849`）；`cost` / `tokens` 按 D13「never treat missing as 0」保持 NULL（`:1841-1843`） |
| 上报契约 + `cost`/`tokens` 落列 | T15 #660 | 与 `duration_ms` **同事务同条**写入（`graph_store.rs:1846-1847` 的 `reported.and_then`） |
| 多步 workflow 的逐步用量 | T18 #668（承 #666 裁决 C） | `steps_payload()` 未门控纯函数（`graph_store.rs:2660`），结果挂到终态事件 `payload["steps"]`（`:1936-1938`）；刻意**不**用逐步记录反推 `usage_reported`（`:1931-1935`） |

### 只有 §21 能回答（空位）

1. Optimizer 要算的**三个比率**分别是什么 —— 分子 / 分母各取自上表哪一列？
2. 输出写到哪里（新表 / 新事件类型 / `payload` 子键）？**谁消费**它？
3. 它是**在线决策**（影响派发或放置）还是**离线统计**（只出报表）？

## 二、P2-2 Blackboard review

### 已确证的前置

| 项 | 票 | 一手锚点（2026-09-18） |
|---|---|---|
| review 成为图节点 | T16 #661（承 D14 #658） | `node_type_for()` 由 capability 派生，`graph_nodes.type` 从此**有写者**（`graph_store.rs:352-358`）；D14 留下的「谁插入 review 节点」取 (b)：想 review 就写带 `review` capability + 依赖边的节点（`:345-351`） |
| 「必须由产出者之外的人执行」 | T19 #670（承 D16 #669 裁决 A） | `requires_independence()` 是**唯一**条件（`graph_store.rs:381-383`），节点打标与网关派发口**共用**它（`:360-367` 明写不要另行推导）；`dispatch_gate` 收 `ReviewIndependence`（`crates/uc-grpc/src/worker_service.rs:319-324`），`placement_target` 带 `exclude`（`:440-447`） |
| 匹配语义 | T19 | **精确相等**，非子串、非忽略大小写：`"code-review"` / `"Review"` 都是普通 subtask（`graph_store.rs:369-371`，测试 `:3893`） |
| 已知未解 | D16 裁决 5 | `required_capabilities` 是 capability 与 `steps[].agent` 的**并集**（`graph_store.rs:373-379`）；`agent == "review"` 会把节点提升为 review 节点。该冲突**已知、刻意不在此解决** |

### 已记账的残余（开票时作输入；两项中一项已于 T32 消除）

- **T19 的 race 窗口**：这道门是**花名册检查**，不是投递保证 —— 候选 ≥2 且含生产者时，共享 work-queue 仍**可能**投给生产者（最坏形状已变成可见的 `PENDING`）。
- **#644 残余 2（清单漂移）** —— **已于 T32 / #682 消除（2026-09-18）**：原先 `ALL_AGENTS` 在测试内
  **手抄**（`tests/python/test_sandbox_env_allowlist.py:51`），与 `python/ultimate_coders/agent/sandbox.py:80`
  的 `ADAPTER_ENV_ALLOWLIST` **平行维护** ⇒ 新增 adapter 不会被自动纳入参数化，且**套件仍全绿**（断言静默缺失）。
  现改为**从 allowlist 自身推导**（`:62`；别名支取 `sandbox.py:28` 的 `GROK_AGENT_ALIASES`），
  并加元测试钉住「必须保持推导」。消融两方向实测：**修复前 + 探针键 = 69 不变**（正是那个失效模式）、
  **修复后 = 70 ⇒ 72**（两个参数化点各 +1，测试文件一字未动）。
  ⇒ 本节剩下的**唯一**活输入是 T19 的 race 窗口。

### 只有 §21 能回答（空位）

1. review **策略**本体：谁触发、何时触发、评审结论如何回到图（回退 / 新节点 / 只记录）？
2. 上面那条 `steps[].agent` 命名冲突是否在 P2 收口（改名要动 wire 与 TS 推导）？
3. 共享队列上的**投递保证**是否在本体范围内 —— 消除该 race 曾是被否决的裁决 D。

## 三、P2-3 market scheduling

### 仓内现状：零坐实（2026-09-18 实测）

`market` 全仓仅 **3 处命中，全部与调度无关**：

- `.claude/skills/trellis-brainstorm/SKILL.md:171`（一行 brainstorm 提示）
- `.scratch/durable-runtime-migration/map.md:19`（P0 地图的 out-of-scope 行）
- `.trellis/spec/guides/cross-layer-thinking-guide.md:134` 与 `scripts/check-spec-refs.py:250`（「marketplace 下载」的通用例子与守卫的豁免串）

`PlacementScore` **0**（除 P0 地图那一行）；`optimizer` 唯一命中是 `graph_store.rs:1545` 的注释「P2 Optimizer owns writers」。
⇒ 评估件「market scheduling 的范围无法在仓内坐实」**至今成立**。

### 空位

1. 「market」在本系统里指什么（多租户配额 / 算力市场 / 竞价调度？）—— **仓内无任何依据**。
2. 它与 `PlacementScore` 的关系（后者在仓内同样 0 命中）。
3. 是否有独立 scheduler 模块，还是挂在现有派发口。

## 四、原文到达后的动作

1. 按 §21 填掉上面所有空位 → 各写一张**决议票**（编号在开票时确认）；
2. 决议 → 实现票 → Trellis 任务目录 → 实现 → 门禁 → 直落 main → 关票 → journal；
3. 本文是**输入**，不是验收口径 —— 开票时以票面 Acceptance 为准。

