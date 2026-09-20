# T42 —— 让「行号引用是否可验证」可见（新增 `UNANCHORED`），并修内容锚把同行路径引用当代码的假阳性

追踪：**#692**。前置：T21–T25（建守卫）、#675（提及）、T39（paths）、**T41（扩到 `docs/**` —— 本票的
缺口正是 T41 实现期亲手撞到的：`p2-recon.md` 的 `:233` → `:250`，靠人工读一手才发现）**。

## 一、缺口（一手读数，HEAD `59509e1`）

守卫只有两条**咨询**判据能对「行号」说话，它们的敏感度**不同**：

| 判据（`scripts/check-spec-refs.py:520-551`） | 看什么 | 对**行号**敏感？ |
|---|---|---|
| `STALE`（`offset is not None`） | spec 行上的符号，其**真实定义行**是否落在被引范围内 | ✅ 是（比较**位置**） |
| `CONTENT_MISMATCH`（`content_ok`） | spec 行引用的**代码**是否在目标文件里出现 | ❌ 否（只到**文件级**） |

两者都不成立时 —— `structural is None`、`best is None`、`content_candidate_count == 0` ——
**行号在同范围内怎么改都恒判 `OK`**：没有第三条判据在场。

**口径 `UNANCHORED`（行号不可检）**，三合一取：

1. 判词 ∈ `{OK, STALE}` —— 目标**已解析、已定位**（`AMBIGUOUS` / 结构失败另有其类，计进来是重复计数）；
2. `content_candidate_count == 0` —— 没有任何被引代码可验；
3. spec 行上的符号**都没有可找到的定义** —— `STALE` 永远不会触发。
   ⚠️ 注意 3 不是「行上没有符号」：符号抽取器会从**路径 token 自身**抠出伪符号
   （`worker.py:524` → `worker`），这类符号永远没有定义 ⇒ 同样不可检。**22 条「无任何符号」只是 60 条的子集。**

全量实测：**refs 134 → 60 条 `UNANCHORED`（44%）**，`.trellis/spec` 57 + `docs` 3。
集中处：`agent-capability-spec.md` 18、`logging-guidelines.md` 11、`error-handling.md` 8、
`type-safety.md` 5、`database-guidelines.md` 4、`state-management.md` 4、`p2-recon.md` 3。

## 二、决定性实验（四部分；A 的主张、B/C 证明探针有效、D 是真实漂移）

| 组 | 做法 | 期望 | 实测 |
|---|---|---|---|
| **A** | 60 条**逐条**把行号改成**同范围内但错误**的值，跑守卫比对 stdout | 不变 | **0/60 发生变化**，rc 亦恒 0 |
| **B** | 把一条引用的**路径**打错（`worker.py:524` → `worker_typo.py:524`） | 守卫必须反应 | **rc 0 → 1** |
| **C** | `error-handling.md:320` 的 `sandbox.py:1007` → `:1324`（符号真实定义处） | 判词变化 | 输出变化、**stale 7 → 6** |
| **D** | `p2-recon.md:67` 的 `check-spec-refs.py:250` → `:290`（**T41 真实漂移过的那条**） | —— | **rc 0、stdout 逐字相同** |

⇒ A 的「没变化」不是探针失效，是**结构性不可检**。每条突变按字节恢复 + 复算 sha256 + 工作树核对。

**D 另外暴露了第二个缺陷**：那条**不在** 60 里 —— 它有 1 个内容候选，而候选是同一行上的**另一个路径**
（`.trellis/spec/guides/cross-layer-thinking-guide.md:134`）。该 span 永不可能出现在目标文件里
⇒ `content_ok` **恒 false** ⇒ 今天那句 `ADVISORY: 1 reference(s) quote code absent from the target`
**是噪音而非信号**。成因：过滤器漏了「引用形状」的 span（`PurePath('x.md:134').suffix == '.md:134'`，
不在 `CODE_EXT` 里；`PATH_SPAN_RE` 也没拦住带 `:line` 的它）。

## 三、判据与契约（本票新增/改变什么）

| # | 契约 | 约束 |
|---|---|---|
| 1 | 新增**咨询类** `UNANCHORED`：默认输出打印计数，`--audit` 列具名清单 | **不进 verdict、不改变任何既有绿红**（与 `STALE` / `CONTENT_MISMATCH` 同级）；`0 structural failure(s)` 不受影响 |
| 2 | **修过滤器**：本身就是 `path:line` 形状的 span 不得作为「被引代码」候选 | 这是**修缺陷**，会**改变**读数：`p2-recon.md:67` 的 `cc` 1 → 0、假 mismatch 计数 1 → 0、`UNANCHORED` 60 → **61**（数字动了就在本票内据实回填） |
| 3 | 语料：**4 条**补符号锚（定义落在被引范围内的那 4 条 —— 唯一能验行号的那种） | `database-guidelines.md:64`（`ShortTermMemory`）、`:131`（`list_keys`）、`hook-guidelines.md:19`（`AgentEventType`）、`:109`（`refresh_heartbeat`） |
| 4 | 语料：`type-safety.md:26` 的 **4 个 `:1` 指针降级为提及** | 引用 `:1` = 模块 docstring 首行，**行号 1 不携带信息**；降级后它们由「提及」判据（`MENTION_RESOLVED`，被检查）覆盖 ⇒ 4 refs → 4 mentions |
| 5 | 判据形式 | 断言**整行相等**（T28 ①），不用子串；`exemption_self_check` 必须仍为 `[]` |
| 6 | 接线 | **不动任何 workflow 的 `paths`**，不动既有 job 的 step（本票的产品是脚本与语料，输入集仍是全仓游走） |

## 四、交付形状

- `scripts/check-spec-refs.py` —— 新增 `UNANCHORED` 计算与两处输出；修内容候选过滤器；
  docstring 的分类表与「能看见什么/看不见什么」段同步（推论 A）。
- `tests/python/test_check_spec_refs.py` —— 合成语料测试 + 真实语料钉值 + 消融见证。
- 语料 5 个文件：`database-guidelines.md` / `hook-guidelines.md` / `type-safety.md`（spec 侧）。
- `.trellis/tasks/…/prd.md`、`implement.jsonl`、`check.jsonl`。

## 五、消融设计（as-built：8 处突变，实测矩阵）

计划只写了 4 处，实做 8 处 —— 因为**计划的第一版有两处不成立**（偏差记在下面）。

| 突变 | 打到哪条分支 | 实测打红的测试 |
|---|---|---|
| **M1** `unanchored = False`（flag 从不置起） | 新咨询类本身 | A / D / 普查 |
| **M2** 口径写成「行上有符号」（丢掉「符号**有定义**」） | 判据口径 | D / 普查（**A 必须仍绿**） |
| **M3** `PATH_SPAN_RE` 丢掉 `md`（缺陷复原） | 内容候选过滤器 | E1 / E2 / 普查 |
| **M4** 让行号对无锚行重新可见 | 必要条件证明的敏感性 | **B** / A / 两条既有测试 |
| **M5** 丢掉「定义是否落在引用范围内」 | 阳性对照 | **C**（独此一条） |
| **M6** 把 4 处语料修复之一改回无名符号 | 语料修复是活的 | repaired / 普查 |
| **M7** 把降级的 `:1` 指针改回来 | 降级是活的 | line_one_pointers / 普查 |
| **M8** 让一行引用的代码在目标里不再出现 | 真仓内容钉 | content（独此一条） |

8 处突变的打红集合**两两不同**（脚本内断言），10 条新测试**每条都至少被打红一次**。
恢复由独立进程 `t42_ablation_verify.py` 复核：4 个被突变文件的活字节 == 快照，
且 8 个突变残留串各出现 0 次（正向串 `refresh_heartbeat`):` 出现 1 次）。

**两处与计划不符（据实记录）：**

1. **计划 M1/M2 的第一版打红同一集合** ⇒ 被脚本自己的「同一集合即装饰」断言拦下。
   原因：合成测试 A 当时用裸基名 `worker.py:2`，提取器会咬出符号 `worker` ⇒ A 与 D 同形，两条口径分不开。
   修法：A 改成**带目录**的路径（`crates/one/target.py:2` ⇒ 行上无符号），于是 **M1 红 A、M2 不红 A**。
   —— 这正是「数分支，不数突变」那条铁律的现场例证。
2. **计划 M3「假 mismatch 计数回到 1」不成立**：光复原过滤器**已经不能**复现症状了。
   本票给守卫写的头注里就含 `md:134` 这个例子，而内容测试只问「这个子串在不在目标里」——
   **记录缺陷的散文把缺陷掩住了**。所以症状侧另配 **M8**（语料侧制造真不匹配）才有私有红灯，
   机制侧由合成测试 **E1** 钉（它在 M3 下必红）。这条限度已写进那条真仓测试的 docstring。

## 六、验收（逐条可复算）

1. `python scripts/check-spec-refs.py` **rc 0**，且输出含 `UNANCHORED` 计数（= 实现后实测值）。
2. `--audit` 的清单条数与计数**逐条相等**（同源，不是两处各算一遍）。
3. 假 mismatch 消失：`0 of N have no matching quoted content`。
4. `pytest tests/python/test_check_spec_refs.py -o addopts=""` 全绿；全量 `1234 collected` 对账。
5. 4 条符号锚修完后，那 4 行**全部 `OK`、`offset=None`、`unanchored=False`**（定义落在引用范围内，`STALE` 计数不变差）；
   4 条降级后 `refs` 134 → **130**、`mentions` 267 → **271**（已实测）。
6. `UNANCHORED` 终态 = **53**。计划估的 57 **偏大 4**，原因已查明：降级的 4 条 `:1` 指针**本身就是无锚的**
   （`types.py:1` 咬出的伪符号 `types` 落在 `SYMBOL_STOPWORDS` 里，且被内容过滤器排除 ⇒ 无符号、无候选），
   所以它们从 `refs` 里消失时**分子分母一起 −4**。⇒ 61 − 4（修好的）− 4（降级的）= 53。
7. 其余守卫全 rc 0；ruff 通过；账本通过。
8. CI：Scripts CI 两腿逐字与本地一致；**每个提交各自等 CI 绿**（T41 的新铁律）。

### 终态读数（实现后据实回填）

HEAD `59509e1` + 本票改动，`python scripts/check-spec-refs.py`（**rc 0**）：

```text
scanned 130 `path:line` references in 13 spec files
scanned 271 line-free path mentions in 32 spec files (205 resolved / 43 dangling / 23 ambiguous)
ADVISORY: 9 bare-basename references are ambiguous (under-specified, not counted as failures)
ADVISORY: 7 references look symbol-stale (run with --audit for the table).
ADVISORY: 53 reference(s) have no checkable anchor (run with --audit for the list).
ADVISORY: 43 line-free path mentions resolve to no file (43 exempt by documented reason, 0 otherwise)
summary: 114 ok / 7 stale(advisory) / 9 ambiguous(advisory) / 0 structural failure(s)
         0 of 130 have no matching quoted content (orthogonal to the verdict above)
         53 of 130 have NO checkable anchor: no symbol with a findable definition and no quoted code, so a changed line number cannot be detected (see UNANCHORED in the module docstring)
         mentions: 43 of 271 resolve to no file (43 exempt by documented reason, 0 unclassified)
spec reference audit passed.
```

| 量 | 实现前（HEAD `59509e1`） | 实现后 | 说明 |
|---|---|---|---|
| `refs` | 134 | **130** | −4：降级的 4 条 `:1` 指针（T41 的实测终值即 134，见 `ci-scripts.yml` 头注） |
| `mentions` | 267 | **271** | +4：同一批，现为 `MENTION_RESOLVED` |
| 无匹配引文（假 mismatch） | 1 | **0** | 内容候选过滤器修好 |
| `UNANCHORED`（新判据） | 60（口径度量，未输出） | **53** | 新可见；见验收 6 的分解 |
| `ok / stale / ambiguous / structural` | 118 / 7 / 9 / 0 | **114 / 7 / 9 / 0** | 实现前由 `134 − 7 − 9` 推得；`stale`/`ambiguous` 未动 |
| 4 条新符号锚 | 无锚（2 条有假内容候选） | 全 `OK`、`unanchored=False` | `ShortTermMemory`@45 / `list_keys`@270 / `AgentEventType`@35 / `refresh_heartbeat`@158 |

## 七、非目标与账（未静默丢弃）

- **不把 `UNANCHORED` 升为结构判据**：60 条存量会让构建立刻红。升不升需要先有存量/成本/收益三方数据。
- **不重写 60 条引用**：其中只有 **4 条**能补出「验行号」的符号锚；其余 56 条若硬补只能得到**文件级**内容锚
  （**仍然验不了行号**）⇒ 有测量依据再动。这是本票**不开那扇门**的理由。
- **记账**：`CONTENT_MISMATCH` 只到文件级 —— 「引用的代码还在文件里，但行号已经指到别处」**依然无判据**。
  本票只是把「连代码都没有」这一类分出来，不宣称解决了前者。
- **记账**：符号抽取器会把路径 token 咬成伪符号（`worker.py:524` → `worker`）⇒ 它让「看起来有锚」的行数虚高。
  本票只在 `UNANCHORED` 的判据里绕开它（要求符号**有定义**），**没有**去修抽取器本身。
