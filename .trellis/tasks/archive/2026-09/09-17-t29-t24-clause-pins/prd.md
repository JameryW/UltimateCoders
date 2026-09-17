# T29 — #677：给 T24 的两处守卫子句补上**测试级**护栏，并更正一处已过期的理由

> 交付物：`tests/python/test_check_spec_refs.py` 新增 1 条钉（合成、确定性）
> + `scripts/check-spec-refs.py` 的两段注释更正。
> **不动判据口径、不删 `EXCLUDE_DIRS` 任何条目、不重跑 T24 的 spec 重写、不碰产品代码。**

## 1. 由来

T24（#673 切片 B）改了守卫两处子句：bold 锚收紧为 identifier-exact、`EXCLUDE_DIRS` 加 `.scratch`。
它的消融（`archive/2026-09/09-16-t24-.../research/notes.md` §E）**只覆盖了第二处**，
且判据是 **CLI 输出差异**（T0/T1/T2，**三次 exit 全 0**）。

⇒ **没有一条测试会因这两处被撤销而变红。** 按铁律「**没有红过的检查器不是证据**」，
两处子句的失效**都没有护栏** —— 任何重构都能静默删掉它们。
本票把「子句生效」（T24 已证）升级为「**子句被钉住**」（本票要证）。

## 2. 一手实测（2026-09-17，`b8e683f` 工作树）

守卫 `39904 B`，sha256 `ddaadb29b048251a`（纯 CRLF，786 CRLF / 0 孤立 LF）。
基线：`11 passed`、守卫 `exit 0`、`89 ok / 1 stale / 8 ambiguous / 0 structural`、mentions `41/227`。

突变全部是**纯删除/替换**、**由正则从文件字节定位**（不写死整行字面量 —— T24 首版正是死在 CRLF 上）。

| # | 突变 | 打红 | 守卫输出 | 判词 |
|---|---|---|---|---|
| MC | **对照**：关掉「裸通配符」不变量（已知被钉住） | **1**（`test_bare_wildcard_pattern_is_reported`） | 无变化 | 夹具**证明能红** |
| MB | 删掉 `if IDENT_RE.fullmatch(b.strip())` 过滤（−33 B） | **0** | **变**：`1 stale → 2 stale`、`89 ok → 88 ok`（2 行） | 🔴 **未钉住** |
| MS | 删掉 `EXCLUDE_DIRS` 里的 `".scratch", `（−12 B） | **0** | **逐字相同** | ⚠️ 判定面效应 **0** |

- 对照的失败集合与 MB、MS **不相交**；三者集合两两不同 ⇒ 无装饰性突变、无相互掩盖。
- 每格突变后按字节恢复并复校 sha256；**最终哈希由另一个进程复算**（同进程的自断言不算证据）。

### 2.1 MB 的机制

删掉过滤后，`error-handling.md:307` 那行的散文词 `delete` 重新成为符号锚。
判定取「**定义离引用行最近**的符号」：`delete` 在目标文件里有定义且比真锚更近 ⇒ 落在引用范围外
⇒ `offset` 非空 ⇒ **假 STALE**。与 T24 记录的 `STALE 27→26 / OK 113→114` 同向同量（现为 `1→2` / `89→88`）。

### 2.2 MS 为什么变成了空操作

`EXCLUDE_DIRS` 在 **git 路径**（第 302 行）上确实生效，`.scratch` 仍在滤掉 **12 个被跟踪的**
`.scratch/durable-runtime-migration/**` 文件。但那 12 个的 basename（`map.md`、`T1.md`…`T7.md`、
`D4-*.md`…`D7-*.md`）与语料里任何提及**都不撞** ⇒ 删掉后输出**逐字相同**。

而 T24 写下的理由 ——

> *"Excluding it changes exactly one verdict in the whole corpus (`event-pipeline-spec.md:153` …)"*

—— 是 **`os.walk` 索引时代**的测量（当时本机 `.scratch/` 里有回滚副本与测试脚手架树）。
**T26 隔天把索引换成 `git ls-files`，local scratch 在构造上就进不了索引** ⇒ 该句已变成假话。
按铁律推论 A（过期文档自带权威感），**同票更正**。

### 2.3 附带发现：`EXCLUDE_DIRS` 在 git 路径上 9/10 是死的

逐条量「被跟踪且后缀在 `CODE_EXT` 的文件数」：8 项为 0；`vendor` 有 1 个跟踪文件但**后缀不在 `CODE_EXT`**；
只有 `.scratch` 有活输入（12）。⇒ 生产索引路径上，10 项里 **9 项无影响**，它们**只对 `_walk_index` 回退路径**有意义
（那里必需：去掉会让 `target/` 之类被整棵走查）。**不是缺陷**，但要写明，否则下一个人会据此「清理」排除表。

## 3. 要改的两处

### 3.1 补钉：`test_bold_prose_is_not_a_symbol_anchor`（合成、确定性）

构造（全部走合成的 tmp ROOT，索引是 `_walk_index` 回退）：

- 目标文件 `crates/one/target.py`：在**远离引用行**处定义 `delete`（如第 40 行）。
- spec 行：一条 `path:line` 引用（`:2`）+ 一个**在目标文件里没有定义**的真锚（反引号）+ 一句**散文** bold
  （形态学同 `delete`，且该词在目标文件里有定义、位置更远）。
- 断言：该 ref 的 `verdict == OK`（真锚解析不出来 ⇒ `best is None` ⇒ 无 offset）。
- **钉住的性质**：一旦散文 bold 被当成锚，`delete` 就会成为 `best` 且落在范围外 ⇒ 该行变 `STALE` ⇒ 测试红。

⚠️ **不钉真语料的具体计数**（如 `== 1 stale`）：那会随任何合法的语料编辑而碎，
且本票的目的是钉**语义**、不是钉**数字**。

⚠️ **新钉自身要做消融**：把 MB 突变打回去，它**必须变红**；未突变时绿。

### 3.2 更正注释（`scripts/check-spec-refs.py` 96–110 行）

改成与现状相符的三条陈述：① 该条目在 git 路径上只影响 12 个被跟踪的 `.scratch/**` 文件，
**当前改变 0 条判定**；② T24 的「1 条判定」是 `os.walk` 时代的测量，T26 换源后不再成立；
③ 其余 9 项只对回退路径有意义。**注释改写不得改变任何行为**。

## 4. 验收（可逐条核对）

1. `pytest tests/python/test_check_spec_refs.py -o addopts=""` → **12 passed**（新增 1 条）。
2. **新钉的消融**：施加 MB 突变 ⇒ 新测试**变红**；未突变 ⇒ 绿。
3. 施加 MB 突变时守卫仍报 `2 stale / 88 ok` ⇒ 新钉钉的正是那条行为。
4. **注释更正后行为逐字不变**：守卫输出（含 advisory 与 summary）与改动前**完全相同**；
   改动仅为注释 ⇒ `git diff --numstat` 为小改动、**0 处非注释行变化**（逐行核对）。
5. 守卫 `exit 0`、`89 ok / 1 stale / 8 ambiguous / 0 structural`、mentions `41/227`（不变）。
6. `ruff check` 两文件 **All checks passed**；两文件 `ast.parse(feature_version=(3,9))` 通过。
7. 守卫按字节复原、sha256 前后一致（由另一个进程复算）。
8. CI：Scripts CI 绿（`scripts/**` 在它的触发面内）；Python CI 因 `tests/**` 触发。

## 5. 非目标

- 不重跑 T24 的 spec 重写（那部分由「零覆盖损失逐条断言」+ 守卫自身数字守着）。
- **不删任何 `EXCLUDE_DIRS` 条目**（回退路径需要它们）。
- 不把 `.scratch` 从排除表移除 —— 那会改变 12 个跟踪文件的可见性，是**决策**不是修复（见 §6）。
- 不改判据口径、不动 `.trellis/workspace/JameryW/` 的账本。

## 6. 已知限制 / 未处置

- **`.scratch` 该不该继续隐形？** 两种说法都站得住：① 保留（现状）—— 防将来 `.scratch/**` 里的跟踪文件
  与basename 撞车；② 移除 —— 跟踪文件就是仓的一部分，守卫本就该看见（T26 建立的原则）。
  本票**保留现状**（行为不变最安全），并把该取舍记在 §2.2 与票面，**不擅自决定**。
- **`EXCLUDE_DIRS` 的 9 项死条目**：只在回退路径活着，本票只写明、不重构。
- **MC 对照在 CLI 上无输出变化**：那条不变量的判据只在 `MENTION_EXEMPT` 表出现坏条目时才触发，
  真语料里没有 ⇒ 它是**纯测试级**的钉。这正是「测试级消融」与「CLI 级消融」判据不同的实例。
