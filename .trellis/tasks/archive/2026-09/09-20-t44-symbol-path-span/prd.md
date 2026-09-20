# T44 把「是不是路径」的两份判据合成一份（#694）

> 承 T42（#692）与 T43（#693）。那两张票都**绕开**了符号抽取器的缺陷（口径写成 `best is None`），
> 留下唯一一笔挂账：`_symbols_on` 仍会把 `path:line` 指针咬成伪符号。

## 背景

同一个模块里对同一个问题有两个答案：

| 位置 | 判据 | 认出 `path:line`？ |
|---|---|---|
| `_symbols_on:486` | `pathlib.PurePath(span).suffix in CODE_EXT` | ❌ 否（`.py:524` ∉ `CODE_EXT`） |
| `_content_anchor:527` | `PATH_SPAN_RE.match(span) or suffix in CODE_EXT` | ✅ 是（T42 把 `md` 加进了 `PATH_SPAN_RE`） |

于是 `` `worker.py:524` `` 不被排除 ⇒ 按 `[.\s()\[\],=:]+` 切开 ⇒ `worker` 存活
（≥4 字符、`IDENT_RE` 全匹配、不在 `SYMBOL_STOPWORDS`）。

T42 为**内容锚**侧写了理由（*POINTER, not quoted code*），但没把同一理由用到符号侧；
T43 的口径注释还把这个咬伤写成 `best is None` 的**理由**。

## 全量读数（2026-09-20，HEAD `5ee7be2`）

`refs 130` / `located 121` / 判词 `114 OK · 7 STALE · 9 AMBIGUOUS`。

| 量 | 值 |
|---|---|
| 带纯伪符号的 ref 行 | **89 / 130** |
| (行, 伪符号, 目标) 三元组 | **115** |
| 其中是目标文件里 `def` 的 | **0** |
| 去重伪符号词 | 14 个（三个口径分别 14 / 15 / 16 种，见已知限制），全部命中 0 |
| 当选 `symbol` 是伪符号的行 | **0** |
| 用忠实补丁重跑后的字段级变化 | **0**（判词/符号锚/UNANCHORED/LINE-UNCHECK 全不变） |
| 注入一个与基名同名的 def 后 | **28 行 OK → 假 STALE**；符号锚 17→45；UNANCHORED 53→45；LINE-UNCHECK 104→76 |
| 修复后的可观测钉值 | 点名行数 **115 → 98**；A/B 分歧 **98 → 81** |

⇒ 定性：**真实缺陷、今天零影响、一个 def 之遥 28 行爆炸半径**。
所以本票的测试必须钉「点名行数 98 / 分歧 81」（语料级可观测）+ 单元级咬伤 +
**危害绊线**（122 个三元组命中 def = 0），而不是钉判词（判词本来就不动）。

## Scope

1. 抽出 `_is_path_span(span)`（`PATH_SPAN_RE.match(span) or PurePath(span).suffix in CODE_EXT`），
   `_symbols_on` 与 `_content_anchor` 都调它 ⇒ 一份实现、两个消费者。
2. `_symbols_on` 的排除动作改用该谓词（三条 span 来源统一）。
3. docstring 同步：`_symbols_on` 增 T44 段；`_content_anchor` 注明共用；
   `line_unchecked` 注释里那句「抽取器会咬出伪符号」改写（修完后为假），
   用 17 行证据说明 T43 口径仍必要。
4. 测试：单元 4 条 + 谓词一致性（带非空性断言）+ 语料钉值 98/81/17/53/104 + 危害绊线。
5. 消融 M1（复原符号侧旧判据）/ M2（把共享谓词收窄回 T42 之前）—— 红灯集合必须不同。

## 验收（与票面逐条对齐）

1. `check-spec-refs.py` rc 0，人类输出与 `--json` 一致，`refs=130`、判词 `114/7/9` 不变。
2. 「是不是路径」只有一份实现 —— Grep 证明 `PurePath(` 与 `.suffix in CODE_EXT` 只出现在 `_is_path_span` 内。
3. 四条单元断言成立（含混合行 `` `Task.to_dict` (`types.py:67-80`) `` 保留 `Task`/`to_dict`）。
4. 语料钉值：点名行数 **98**、A/B 分歧 **81**；符号锚 17 / UNANCHORED 53 / LINE-UNCHECK 104 逐字不变。
5. `test_check_spec_refs.py` 全绿、收集数只增不减；M1/M2 红灯集合互不相同，每条都
   「先变红 → 按字节复原 → 另一进程复算 sha256」。
6. 危害绊线：`len(triples) == 122`（非空性）且 `hits == []`；测试自带路径词抽取器，
   不用 `_symbols_on`（否则构造上恒空）。
7. Python 全量 pytest 基线不降（1243 passed / 10 skipped）。
8. 每笔提交各自 CI 绿。

## 非目标

- 不改 spec/doc 正文（104 条行号不可检的引用不重写）。
- 不动结构判据（`MISSING_FILE` / `PATH_FORM` / `OUT_OF_RANGE`）。
- 不改 `PATH_SPAN_RE` / `REF_RE` / `CONTENT_TOKEN_RE` / `DEF_PATTERNS` 的图案本身。
- 不新增守卫类、不改 workflow `paths`、不动 T42/T43 归档件。

## 已知限制（如实记账）

- 语料上「判词零变化」是**预期的**，不是没生效 —— 效应面只在「这一行点了名没有」。
  任何只钉判词的测试对本票都是空转，收口时不要用它当证据。
- 危险量（28 行）来自**注入实验**，不是真实语料状态；测试钉的是「122/0 且非空」这个
  **前提**。注入实验实测过绊线会在 **28 处**变红（`hits` 恰为 28 条 `worker`），但那个
  28 不能进断言 —— 语料里现在没有 `def worker`，写了就是钉一个不存在的状态。
- **三个口径必须一起读**（否则会撞上「两个数各自都对却没人说明」）：
  ①「span 整段就是一个路径」= 115 条 / 15 词种（修前抽取器自己的答案）；
  ②「剪掉路径 span 后做减法」= 115 条 / 14 词种；③最宽（token 出现在 span 里任意位置、
  稀疏三源、不做减法）= 122 条 / 16 词种。①与②**总数相同**却在组成上各差 5 条，
  那个相同是巧合 —— 绊线取最宽的③，命中 def 三个口径都是 0。
- 谓词一致性断言若两个消费者都退化，仍可能「一致」⇒ 必须配非空/绝对钉。
