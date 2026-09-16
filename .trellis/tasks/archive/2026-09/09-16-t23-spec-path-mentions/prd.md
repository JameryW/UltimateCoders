# T23 #675 — 守卫新增「无行号路径提及」类：让 47 处悬空提及可见

**Status**: in progress
**Implements**: #675 的**切片 A**（诊断）；47 处的分类与处置留给切片 B
**Unblocks**: **#673 切片 B**（符号优先重写）—— 重写会把 52 处引用变成**无行号**形态，正好落进本票补上的视野
**Related**: #672 / #673（T21 / T22），T22 `research/notes.md`

## 目标

让 `scripts/check-spec-refs.py` 看得见「**无行号的路径提及**」，并把悬空的那批**量出来、列出来**。
本票只做**诊断**：**advisory，不改 exit code**。

## 侦察结论（可复算，命令见 `research/notes.md` §A–§D）

**A. 盲区的规模。** 反引号包裹 + 路径形状 + **围栏之外** + 无 `:行号` = **171** 处提及：

| 解析结果 | 数 |
|---|---|
| 精确命中 / 唯一 basename | 55 / 47 |
| basename 歧义 | 22 |
| **解析不到任何文件** | **47** |

⇒ 守卫今天报「148 refs / 0 structural failures」的同时，**47 处提及指向不存在的路径**。

**B. 47 处是混合的，不是「47 个缺陷」。** 两个**正交**分布轴（⚠️ **会重叠，不可相加**）：

- **按 spec 文件**（主轴）：`tui-grpc-spec.md` **27** · `scheduler-spec.md` 5 · `directory-structure.md` 3 ·
  `cross-layer-thinking-guide.md` 3 · `local-worker-bridge-spec.md` / `nats-bridge-spec.md` /
  `quality-guidelines.md` / `taskservice-grpc-spec.md` 各 2 · `dashboard-spec.md` 1（合计 47）。
  ⇒ **一篇 spec 独占 27 处**，而它描述的对象（`tui/` 项目）**根本不在本仓**。
- **按路径前缀**（副轴）：裸 basename 26 · `tui/` 12 · `tests/` 4 · `python/` 3 · `new_module/` 1 · `crates/` 1（合计 47）。
- ⚠️ 两轴**重叠**：`tui-grpc-spec.md` 那 27 处里多数写作裸名 ⇒ 前缀轴只数出 12 个 `tui/`。
  **不能相加** —— 本票首版就是这么写错的（见 `research/notes.md` §F 第 7 条）。
- 其中**已实证的真过期**：`python/ultimate_coders/agent/scheduler.py`（`agent/` 下已无此文件，
  scheduler 现落在 Rust：`crates/uc-engine/src/scheduler/`、`crates/uc-python/src/scheduler.rs`）、
  `python/ultimate_coders/local_worker.py` 与 `crates/uc-grpc/src/local_worker.rs`（子系统已移除，
  `local-worker-bridge-spec.md` 整篇在描述它）、`tests/python/test_agent.py`（无同名测试文件）。

**C. 因此不能直接门禁。** 47 里含：运行时由用户提供的配置（`uc.scheduler.yaml`）、
文档**举例**路径（`new_module/impl.rs`）、命名规范表里的**示例名**（`rate_limiter.py`）、
**仓外**项目路径（`tui/**` 27 处）。朴素门禁会当场红 47 处而大部分不是缺陷
（与 T21 的 63 报 / 61 误报同型）。

**D. 本票顺带修正了 #673 的一个数字。** #673（及 T22 的 notes）写「**84 / 148 今天就能去行号**」，
那是**存在性**口径（本行有符号 或 有内容即可）。加一道**唯一性**检验后：
内容锚里 **32 / 41 的匹配字面量在目标文件里出现 >1 次**（`abort_on_failure` 在 `agent.rs` 出现 **14** 次、
`retry_count` **15** 次、`parallel_group` **20** 次）⇒ 去掉行号后无法区分「指的是哪一处」。
⇒ **安全可去行号 = 43（符号锚）+ 9（唯一内容锚）= 52**，不是 84。

## 变更

`scripts/check-spec-refs.py`（单文件）：

1. 新增**提及**扫描：反引号包裹的路径形状 span、**无 `:行号`**、**在代码围栏之外**。
   围栏必须排除 —— 否则目录树/示例块里的路径会成批进来（实测围栏内另有 73 个路径 token）。
2. 提及复用既有解析口径（精确 / 唯一 basename / 歧义），新增判定 `DANGLING`（解析不到任何文件）。
3. 输出：正常运行时打印 mention 的三个计数（resolve / dangling / ambiguous）；`--audit` 列出悬空清单。
4. **exit code 不变**（提及不是结构性失败），并在 docstring 里**写明范围与理由**。

## 验收

1. 守卫输出能打印提及计数与悬空数；`--audit` 列出悬空清单（逐条 `spec:line` + 路径）。
2. **exit code 与落地前一致**（0），`148 refs / 0 structural` 不变 —— 提及是 advisory。
3. 消融（一次一处突变、字节恢复、逐条断言）：
   - M0 基线复现；M1 往 spec 追加一条**悬空**提及 ⇒ dangling **+1** 且 **exit 仍 0**（证明 advisory）；
   - M2 同一条放进**代码围栏**内 ⇒ dangling **不变**（证明围栏排除）；
   - M3 追加一条**存在**的提及 ⇒ dangling 不变、resolve **+1**；
   - M4 追加 `path:行号` ⇒ 计入 **refs** 而**不计入** mentions（证明两类不重复计数）。
4. 突变后按字节恢复（校 sha256），并复查 refs/mentions 计数回到基线。
5. 提及计数可被**独立复算**（本票的 `.scratch` 侦察脚本与守卫两路一致）。

## 非目标

- **不改 spec 正文**（47 处的分类与处置 = 切片 B）。
- **不改** `path:line` 既有判定与 exit code。
- **不实现**锚点门禁（提及只判解析，不判锚 —— 锚门禁是切片 B/C 的事，需要它自己的消融）。
- **不接 CI**。
