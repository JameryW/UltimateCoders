# T20 #671 — 能力层规范与代码脱节：纠正幽灵 API，并把默认能力事实钉死

**Status**: delivered
**Implements**: —（无决策票：无产品取舍，纯「文档与代码一致性」维护）
**Map**: —（不属 P2 范围；本票是仓级文档卫生，见「与地图的关系」）
**Related**: #670（T19 —— 本票补它收口时漏掉的一处同源假话）、#111（`ad931ec`）、#161（`05ccb56`）

---

## Goal

把 `.trellis/spec/backend/agent-capability-spec.md`（987 行）里描述**已被删除的层**的契约，纠正为**代码事实**；并把「worker 默认能力」这条最承重的事实从「被规范描述」升级为「被测试钉住」。

## 背景：这不是整洁问题，是同一个失效面

本仓最贵的错法是**把间接信号当事实**，而「过期规范」是其中最危险的一种 —— 它自带权威感。2026-09-16 一天内同源错误出现三次，其中一次（T19）正是**同一份文件的同一类陈述**：规范写 worker 默认能力集**包含** `review`，读码才知道 `review` 要 `UC_CAP_REVIEW` 显式 opt-in（`worker.py:448` / `:495`）。照规范推理会得出「T19 让单 worker 部署永久卡住 review 节点」这个耸动而**错误**的结论。

T19 当时把那份规范修好了**一处**。本票的侦察发现：**同一份文件的前 250 行整体描述着一层已经不存在的代码**，且那层已消失约三个月。

## 侦察结论（全部一手取证，可复算）

### A. §1/§2 的六个签名全部是幽灵

`git grep` 全仓（tracked）+ `python/` `packages/` `crates/` 三树逐符号命中：

| 签名 | 规范行 | 命中数 | 移除于 |
|---|---|---|---|
| `Worker._self_evaluate` | 22 | **0** | `ad931ec` 2026-06-21 (#111) |
| `Worker._classify_error` | 47 | **0** | 同批 |
| `Worker._adaptive_retry` | 62 | **0** | 同批 |
| `Orchestrator._select_worker` | 77 | **0** | `05ccb56` 2026-06-26 (#161) |
| `Orchestrator.schedule_subtasks` | 87 | **0** | 同批 |
| `Worker._gather_prior_context` | 99 | **0** | 同批 |

`_select_worker` 全仓唯一命中就是该规范自己。§1 第 12 行的 Scope 清单、§5 的 Good/Base/Bad（156–175）、§7 的 Wrong/Correct（226–254）全部建立在这六个函数上。

### B. §3 的契约同样建在死码上

| 概念 | 规范行 | 实际 |
|---|---|---|
| `_record_experience` | §3.3 / §7 | 仅该规范 + 一份**已归档**的 2026-06 任务 prd |
| `confidence_threshold` | §3.1 | 全仓 **0** |
| `experience_key` | §3.3 | 全仓 **0** |
| `FALLBACK_TOOL` | §3.2 / §7 | 只剩 `types.py:61` 一个枚举成员，**零消费者** |

### C. §6 点名的两个测试不存在

`test_select_worker_capability_match`（第 187 行）与 `test_select_worker_fallback_load`（第 188 行）—— 全仓唯一命中就是这张表本身。

### D. 同文件两处「默认能力」事实错误（与 T19 修的那处同类）

- 第 606 行：`test_default_capabilities` 记为 *"Base caps: code, search, memory, test"* —— 漏 `decompose`。
- 第 645 行：反例代码 `capabilities or ["code","search","memory","test"]` —— 同样漏 `decompose`。

⚠️ 且有**第二层错**：把 *base seed* 当成 *advertised set*。见下方「一次自我更正」。

### E. T19 的收口遗漏（同源假话仍在）

`tests/python/test_worker_capabilities.py:151` docstring：*"The dispatch side has no exclusion primitive (research/notes.md §3)"* —— 自 T19 起不成立。T19 改了 `worker.py` 里的同源注释，**漏了这一处**。这正是 T19 自己写下的那条铁律（推翻结论时要更正**所有**已落盘的记录）的反例。

### F. 广告词未更新

`.trellis/spec/backend/index.md:37` 仍以 *"Worker self-reflection, adaptive retry, capability matching, scheduling, experience recall"* 描述该规范并标 `Filled`。

## 一次自我更正（本票最重要的方法论副产品）

侦察早期我从源码 `worker.py:448` 读到 `caps = ["code","search","memory","test","decompose"]`，**推断**默认能力就是这 5 项，并打算据此写等值断言。**实跑推翻**：

```
Worker(worker_id='probe-default').capabilities ==
['code','search','memory','test','decompose','grok-build','grok','claude-code','codex',
 'deepseek-harness','deepseek','local-harness','local-llm']      # 13 项
```

多出的项来自 `worker.py:524` 的 `agent_registry.registry.capability_names(shutil.which)` ⇒ **随注册表与 PATH 变化**。

⇒ 两处结论：① 规范的「四元素列表」有两个层次错（漏 `decompose` + 混淆 seed 与 advertised）;② **若我按读码结果写等值断言，那条测试会当场打红，而我会去改一个没坏的东西**。这正是「读一手来源，或直接跑一次」这条铁律的现场案例 —— 源码片段是**间接信号**，运行结果才是事实。

## 设计

### 规范改写原则（三选一的取舍已定）

死契约**删除**，不保留为「历史设计」。理由：本仓已明确「留着一个指向不存在事实的路标，等于给下一个人埋雷」（T19 口径）。但**留 2 行留痕**：写明该层于 #111/#161 移除、能力决策的现行收口点在哪（Python 侧 `_derive_capabilities` ∪ 注册表；派发侧 Rust `workers_with_capabilities_excluding`）。**不臆造**替代设计。

### 默认能力的正确表述（两层分开）

- **base seed**（`worker.py:448`）：`["code","search","memory","test","decompose"]` —— 固定、可钉。
- **advertised set** = seed ∪ MCP/工具派生 ∪ 插件注册表派生 ⇒ **环境相关、不可等值钉**。
- `review` **不在** default，须 `UC_CAP_REVIEW` opt-in（T16/D14 的独立性前提）。

### 钉法

`test_default_capabilities` 由成员资格断言升级为**绝对钉**，按 T19 的教训（松散断言什么都钉不住）：base seed 必须含 `decompose`、必须**不含** `review`、且 registry 派生项不得冒充 seed。**不断言**环境相关的注册表项。

## Scope（编号，与 issue #671 对齐）

1. `agent-capability-spec.md`：§1 Scope / §2 Signatures 改写为代码事实 + 留痕。
2. 同文件：§3 契约中 `self-evaluate` / `FALLBACK_TOOL` / experience memory 三条死契约处理。
3. 同文件：§5/§6/§7 中引用死函数与不存在测试的部分纠正。
4. 同文件：两处默认能力事实（第 606 / 645 行）修正为 seed/advertised 两层表述。
5. `index.md:37` 描述修正。
6. `tests/python/test_worker_capabilities.py` docstring 修正（T19 遗漏项）。
7. `test_default_capabilities` 升级为绝对钉。
8. 消融自检：一次删一条被钉的子句、确认变红、恢复。

## 验收

1. 全仓 `git grep` 对六个死符号 + `_record_experience` / `confidence_threshold` / `experience_key` / 两个不存在测试名的命中，**只应剩规范里的「已移除」留痕**（即这些名字要么消失，要么只出现在明确标注移除的句子里）。
2. 规范里对默认能力的每一处陈述，都与 `worker.py:448` 的 seed 及实跑输出一致（seed 5 项含 `decompose`；`review` 须 opt-in）。
3. `index.md` 不再以已删除的能力描述该文件。
4. `test_worker_capabilities.py` 里不再有「派发侧没有排除原语」这类已失效陈述。
5. `test_default_capabilities` 能在删掉 `decompose` 或加入 `review` 时**变红**（消融实测，非推断）。
6. 受影响 pytest 文件逐文件串行全绿；`ruff check` clean。
7. Rust / TS 零改动 ⇒ 不触碰其基线（本票**不应**改变任何 Rust/Python 测试计数，`test_default_capabilities` 是**改断言**不是加用例 —— 计数须逐项不变，这本身就是一条回归证据）。

## 交付中补齐的遗漏（票面清单外，显式记账）

验收回读时逐符号重扫全仓，发现 `_gather_prior_context` 还在**第二份活规范**里：

`.trellis/spec/backend/codegraph-integration.md` —— 第 11 行声称 Worker 有 "Pre-processing layer"（自动查 codegraph 注入上下文），
第 81 / 122 行把 `_gather_prior_context` 当现行契约。

一手取证（**过程中纠正了自己一个错误推断**）：

| 待证 | 证据 | 结果 |
|---|---|---|
| 该函数何时移除 | `git log -S "_gather_prior_context" -- python` | **`ad931ec` (#111)** —— 我起初以为是 #161，**错了** |
| worker 还调 codegraph.explore 吗 | `git grep -n "\.explore(" -- python` | 零调用；唯一命中是 `codegraph.py:33` 的 docstring 用法示例 |
| 回退串还在吗 | `git grep "prior context not gathered"` | **零命中** ⇒ 该节**两半都过期** |

处理方式与取舍：**只加 15 行横幅，不改写正文**。理由：改写该节需要先重审整份规范（连同 ADR-1 的 "Mixed Architecture" 两半），
那是 #672 的范围；而横幅的每一句都可独立验证（移除提交、零命中、存活的 docstring、消失的回退串），**不臆造替代设计**。
⇒ 本票的验收口径相应收紧为：**死符号只允许出现在「已标记移除/已标记过期」的区段内**。

## 验收回读（交付后逐条核对，非仅执行前声明）

| # | 验收 | 实测 | 结论 |
|---|---|---|---|
| 1 | 死符号只出现在标记移除区 | 六个符号全仓命中 = 本规范 History 块quote + 已归档 prd（历史件）+ codegraph 规范的**已标记横幅段** | ✅ |
| 2 | 默认能力陈述与代码一致 | seed 5 项 `worker.py:451`（含 `decompose`）；实跑 advertised 13 项；`review` 须 `UC_CAP_REVIEW` | ✅ |
| 3 | `index.md` 不再以已删除能力描述 | 改为「Capability derivation and advertising; the `review` opt-in; pointer to dispatch-side routing」 | ✅ |
| 4 | 测试里不再有失效陈述 | docstring 已改为「自 T19 起另有派发侧排除集」（只更失效半句） | ✅ |
| 5 | 新断言能被突变打红 | 消融：删 `decompose` ⇒ RED；塞 `review` ⇒ RED（基线 GREEN） | ✅ |
| 6 | 受影响 pytest 全绿 + ruff clean | 28 passed / 187 passed；`ruff` All checks passed | ✅ |
| 7 | **不改变任何测试计数** | 逐文件收集 **1149**（44 文件、零条目 0），与 T18/T19 基线逐项相同 | ✅ |

⚠️ 验收 7 的取证本身踩了一次同源坑：初版汇总脚本报 **1146**（其中一个文件的收集数没被正则匹配到、静默计 0），
靠对账不平发现，再**换一种机制**（数测试 ID 行、不解析汇总行）独立复算得 1149。**自写的汇总脚本也是间接信号** —— 这正是 T19 记下的铁律。

⚠️ 一处刻意的**范围克制**：`types.py:61` 的孤儿枚举成员（`FALLBACK_TOOL` 等）**不删** —— 产品面存废另议，本票只停止把它描述为现行契约。

## 非目标

- 不动第 255 行之后的 OMP/TS、沙箱定制、多步 workflow 章节（`_derive_capabilities` / `_resolve_agent_config` / `_merge_agent_config` 经核实仍在用）。
- **不删除** `types.py:61` 的孤儿枚举成员 —— 产品面存废另议（本票只停止把它描述为现行契约）。
- 不做全仓 spec 普查（8,842 行 / 31 文件）；只覆盖已逐条取证的范围。
- 不改任何运行时行为。

## 与地图的关系

本票**不挂** P2 地图 #656：它不是 P2 三项（Execution Optimizer / Blackboard review / market scheduling）的一部分，也不是其前置。它是仓级文档卫生的独立维护票。唯一的地图关联是 **#670**：本票范围 6 是该票收口的补漏。
