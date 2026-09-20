# T43 让「行号是否被检查」的口径完整（#693）

> 承 T42（#692）：那张票新增了 `UNANCHORED` 咨询类并给出 **53 of 130**。本票是**对它的更正** ——
> 那个数字的两条判据（`not content_candidates and best is None`）比它自己声称的性质窄。

## 背景

`scripts/check-spec-refs.py` 有两条**咨询**判据，也是唯一两种能对「行号」说话的证据：

| 判据 | 看什么 | 对行号敏感？ |
|---|---|---|
| `STALE` | spec 行上的符号，其**定义行**是否落在被引范围内（`:564`） | ✅ 是（比较位置） |
| `CONTENT_MISMATCH` | spec 行引用的代码是否在**目标文件里出现**（`_content_anchor`，`form in body`） | ❌ 否（**只到文件级**） |

T42 的口径 `unanchored = not content_candidates and best is None` 要求**两条都不成立**。
但守卫打印给读者的是「`so a changed line number cannot be detected`」，`--audit` 头部更强：
「`their line numbers cannot be **verified at all**`」。`at all` 是**完备性断言**，而它不成立 ——
一条**只有内容锚**的引用（`cc > 0`、`best is None`）行号改了也**不会**产生任何输出差异，
因为内容锚是文件级子串测试，**签名里没有行号**。

T42 票面自己的对照表就写着内容锚「❌ 否（只到文件级）」，计划第 5 条甚至写了
「其余 56 条若硬补只能得到**文件级**内容锚（**仍然验不了行号**）」——
**结论在票面上，口径里漏了。**

## 全量读数（2026-09-20，HEAD `8952b47`）

`located` = 判词 ∈ {OK, STALE} = **121**（114 OK + 7 STALE）；refs 总 **130**；mentions **271**。

| 组 | 判据 | 条数 | 判词 | 行号被验证？ |
|---|---|---|---|---|
| A | `best is None` ∧ `cc == 0` | **53** | 全 OK | ❌（= T42 的口径） |
| B | `best is None` ∧ `cc > 0`（仅内容锚） | **51** | 全 OK | ❌（**T42 漏掉的**） |
| C | `best is not None` | **17** | 10 OK + 7 STALE | ✅（**唯一会做位置比较的那类**） |

⇒ 行号不可检 = A + B = **104 of 121（86%）**；被真正检查过的只有 **17/121**。
账目闭合：基线的 **7 条 STALE 全部落在 C 组**（P 实验的副产品，见下）。

## Scope

1. **补全口径**：新增 `line_unchecked = best is None`（限制在 located），即「没有任何东西会把这个
   行号与目标比较」。**保留** `unanchored`（= 53）作为其**下位**层级，语义与名字都不变
   （「连锚都没有」）。
2. **输出措辞回到事实**：默认 summary 打印 `line_unchecked`（主数）与 `unanchored`（下位数）并
   说明层级；`--audit` 头部删掉 `at all` 这类完备性措辞，分层列出并把 53 标出来。
3. **docstring**：`UNANCHORED` 条目里那句「and its line number is unverifiable by construction」
   收窄为「是 `LINE-UNCHECKED` 的下位情形」；新增 `LINE-UNCHECKED` 条目，写入三条消费点、
   `_content_anchor` 的文件级性质，以及本票的四件套证据。
4. **测试与钉值**：真实语料钉 `line_unchecked == 104` / `unanchored == 53` / C 组 `== 17`；
   合成测试断言两个口径的**差恰为 51** 且该差集**全部是 OK 且有内容锚**；
   补一条**同一突变在两个口径下的可见性对照**。
5. **消融自检**：两处单点突变（`line_unchecked` 谓词、`unanchored` 谓词）必须打红
   **互不相交**的断言集合。
6. **记账**：T42 落下的那个 53（docstring / 测试 / memory）要**带上口径**，
   避免下一轮继续把它当「行号不可检」的总量。

## 验收（票面 #693 原文）

- 计划 1：`line_unchecked` 存在、限制在 located、`unanchored ⟹ line_unchecked` 成立；
  `line_unchecked ∧ ¬unanchored` 恰为「仅内容锚」那 51 条。
- 计划 2：默认输出的措辞不含完备性断言，且两个数都在；`--audit` 清单条数与计数逐条相等（同源）。
- 计划 3：docstring 两个条目各自可读、无互相矛盾；`md:134` 那条 T42 的说明保留（它是判据，不是叙事）。
- 计划 4：pytest 全绿且总收集数只增不减；新钉值 104/53/17 在真实语料上实测命中。
- 计划 5：M1、M2 两条突变的**私有红灯集合互不相交**（否则其中一条是装饰）。
- 计划 6：`MEMORY.md` 与 `topics/spec-ref-hygiene.md` 里的 53 都带上「完全无锚」这个限定。

## 决定性实验（写进 docstring 的四件套）

### ① 静态：`start`/`end` 的全部消费点

| 行 | 用途 | 位置敏感 |
|---|---|---|
| `:548` | `start > count or end > count` → `OUT_OF_RANGE` | ❌ 仅**上界** |
| `:559` | `distance`，用于**排列**多个符号候选 | ❌ 仅排序（且仅在 `best` 非空时有意义） |
| `:564` | `not (start <= definition <= end)` → `offset` → `STALE` | ✅ **全仓唯一的位置敏感比较**，包在 `if best is not None` 里 |

`_content_anchor(spec_line, body)` 拿**整个文件体**做 `if form in body` —— **签名里没有行号**。

### ② 阳性对照（P 组，证明机械会响）

17 条符号锚 located 引用，整段移到**不含其定义行**的在范围内位置（宽度不变）：
**CHANGED = 10 / 17**，且**每一个**差异都是 `7 references look symbol-stale` → `8`
⇒ 变化**来自符号锚开火**，不是越界假阳性。
余 7 条不改判词的原因已查明：**它们就是那 7 条 STALE**（改后仍 STALE、计数不变，
而默认输出不打印 `offset`）。

### ③ 主张（W 组，51 条仅内容锚）

保持宽度、在范围内挪到**离原 start 最远**的位置：**invisible = 51/51**（rc 与 stdout 逐字节相同）。
⇒ 加上 ②，「全 invisible」**不是探针失效**（同一次运行里同一套机械打红过 10 条）。

### ④ 自更正：第一版探针是错的

首版用 `count = len(spec_text.split("\n"))`（**spec 文件**行数）而不是
`_line_count(row["target"])` ⇒ W 组把 `step_condition.py`（**291 行**）的引用挪到 **833**
（= spec 行数）⇒ 越界 ⇒ `OUT_OF_RANGE` ⇒ **假 CHANGED ×7**；P 组 13/17 被 `skip` 吞掉。
修正后（每次断言 `1 <= ns`、`ne <= tc`）才是 ②③ 的读数。
**教训：探针本身要过一遍「新值在哪个坐标系里合法」的自检。**

## 非目标

- **不把两个口径升为结构判据**：104 条存量会让构建立刻变红；升不升需要独立的存量/成本/收益测量。
- **不重写这 104 条引用**（B 组硬补符号锚多数补不出来，T42 已实测 4/60 可补）。
- 不动 `.trellis/spec` 的判词与阈值；不动任何 workflow 的 `paths`。
- 不改 `AMBIGUOUS` / `DANGLING` 等其它咨询类。
- **不改 T42 已归档的 prd**（归档件是历史账本；更正在本票的 prd + 守卫 docstring + memory 三处）。
