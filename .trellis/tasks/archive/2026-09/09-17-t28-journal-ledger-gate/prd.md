# T28 — #676：journal 账本收口检查 + 回填 S16/17/18 的 `### Git Commits`

> 交付物：`scripts/check-journal-ledger.py` + `tests/python/test_check_journal_ledger.py`
> + `.github/workflows/ci-journal.yml` + `.trellis/workspace/Jamery Wang/journal-1.md` 三处回填。
> 不动任何产品代码；不重跑历史门禁。

## 1. 由来

#674（T27）清掉了 journal 账本的**存量**欠账（39 行占位符 + 5 处重复残块），
但**产量还在**：`.trellis/scripts/add_session.py` 的骨架**每次都**产出占位符
（`### Testing` 段的 `- [OK] (Add test results)`、`### Next Steps` 段的
`- None - task complete`、`### Main Changes`/`### Summary` 的 `(Add details)` / `(Add summary)`），
填掉它们靠自觉 —— 所以 T27 才会积累出 39 行。**T27 只清了存量，没有任何机制阻止它再长出来。**

同时 T27 故意留了一处不做：**S16/17/18 段内没有 `### Git Commits`**（补表 = 猜「哪些提交属于该 session」）。

#676 要求两件事都做掉。

## 2. 票面两处前提被 recon 推翻（一手实测）

| 票面说法 | 实测 | 影响 |
|---|---|---|
| 「两份 journal」（`journal-1.md` / `journal-2.md`） | git 跟踪 **5 份** journal、**2 本账**：`Jamery Wang/`（30 session）+ `JameryW/`（123 session，3 份文件，**363 处占位符**） | 直接对全语料收口 = **一条永远红的检查**（正是 #676 自己警告的形状） |
| 「三个 session 的日期是 2026-09-16」 | 三段的 `**Date**` 都是 **2026-09-15** | 按日期回溯会拉错窗口 |

⇒ 收口范围必须是**可计算且 CI 稳定**的：见 §4、§5。

## 3. 判据：整行相等（不是子串）

记录本票的 session 正文**本身就在引用这三个占位符字符串**（它们就是主题）
⇒ `placeholder in segment` / `text.count(...)` 在**干净的账本上也会报命中**。
一手数字（`journal-2.md`，2026-09-17）：

| 判据 | `- [OK] (Add test results)` | `- None - task complete` | `(Add details)` |
|---|---|---|---|
| 宽松（子串） | 1 | 3 | 4 |
| **严格（`line.strip() == marker`）** | **0** | **0** | **0** |

⇒ **用子串做门禁会得到一条永远红的检查，然后被关掉 —— 那比没有检查更糟。**
代价是一条文档规则：**引用占位符时给它加前缀**（列表符/反引号/表格竖线），别写成独立一行；
违规信息里会直接写出这条规则的解法。

## 4. 守卫 `scripts/check-journal-ledger.py`

### 4.1 检查项（FAIL ⇒ exit 1）

| kind | 判据 | 落进去的历史形态 |
|---|---|---|
| `PLACEHOLDER` | 存在**整行 == 已声明占位符**的行 | #674 的 39 行 |
| `HEADING_COUNT` | 每个 session 的 6 个标准标题**各恰好一次** | 缺 → S16/17/18；重复 → S12/13/14/15 的骨架尾巴 |
| `SESSION_NUMBER` | `## Session` 标题必须带编号，且编号不得重复 | S19 曾被写错成 16（`5354b1c` 修正） |
| `EMPTY_CORPUS` | 没有 journal，或某份 journal 里没有任何 `## Session` | 防「空语料静默通过」 |
| `NO_INDEX` | `git ls-files` 问不出来 | 索引不可用时必须 fail closed |
| `STALE_SKELETON` | 已声明的标题/占位符在 `add_session.py` 里**找不到了** | **检查器看自己**：骨架改名后判据必须立刻失效而不是静默空转 |
| `LEGACY_DRIFT` | `LEGACY_JOURNALS` 里的文件与其 pin 数字不符 | T26 判据 1：「声明的计数必须与语料相符」 |

### 4.2 只报不判（ADVISORY，永不影响 exit code）

- 行尾不统一（有孤立 LF）；
- `index.md` 的 `Total Sessions` 与实测 session 数不一致。

### 4.3 索引源 = `git ls-files`（不是 `os.walk`）

- `.trellis/.developer`（记录「当前开发者」的文件）是 **gitignore 的** ⇒ 干净检出里
  `get_developer()` 返回 `None` ⇒ **「当前这本账」在 CI 里不可计算**。
- 本机用文件系统走查会看见**未被跟踪**的文件（本地新建的开发者目录等）⇒
  同一个提交在本机与干净检出给出**两个判词**（T25 的 `40 exempt` 就是这么来的，
  T26 为 `check-spec-refs.py` 修掉了这个性质）。
- ⇒ 语料 = **git 跟踪的全部 journal**，外加 §5 的 pin 表。

### 4.4 `LEGACY_JOURNALS`：另一本账被**计数并冻结**，而不是被无视

`.trellis/workspace/JameryW/` 是**已入库**的第二本账（最后活动 2026-08-06，在 T 系列之前）。
#676 的范围是交付流程写的那本账，因此**本票不清它** —— 但也不静默忽略：
数字被 **pin** 住，pin 一旦对不上就 FAIL。计数过的债是可见的，没计数的不是。

```python
"JameryW/journal-1.md": {"placeholders": 165, "sessions": 56, "headings_ok": 56},
"JameryW/journal-2.md": {"placeholders": 168, "sessions": 57, "headings_ok": 56},
"JameryW/journal-3.md": {"placeholders": 30,  "sessions": 10, "headings_ok": 10},
```

- pin 只对**表内路径**生效；pin 目录下**新增**的 journal 文件仍按「我们的」判 ⇒ 必须干净。
- 该账的 363 处占位符与 3 处重复编号（49/74/98）→ 见 §7「未处置」。

### 4.5 刻意**不**门禁的骨架产物

`(No commits - planning session)` 与 `[OK] **Completed**` 是**合法内容**
（规划期确实没有提交；`Completed` 是真实状态）⇒ 门禁它们只会再造一条永远红的检查。
同一个产出者、不同含义 —— 这个不对称是**故意**的，写在模块 docstring 里。

## 5. CI 落法：独立小 workflow

`.github/workflows/ci-journal.yml`（**不改任何既有 workflow 的 `paths`**），
Python 3.9 + 3.12 矩阵，三步：`ruff check` 两个文件 → 真语料跑守卫（`--verbose`）→ `pytest`。

**这里用 `paths` 过滤是正确的**（与 `ci-scripts.yml` 刻意不过滤相反）：那个守卫走查全仓，
过滤会瞎；本守卫读的是**封闭输入集**，且全部列进 `paths`：

| path | 为什么必须在里面 |
|---|---|
| `.trellis/workspace/**` | 语料本身（增删任何文件都会改变 `git ls-files` 的结果） |
| `.trellis/scripts/add_session.py` | 声明的标题/占位符就是照它校验的；骨架改名而本 workflow 不重跑 ⇒ 门禁会**静默空转** |
| `scripts/check-journal-ledger.py` | 检查器本体 |
| `tests/python/test_check_journal_ledger.py` | 它的护栏 |
| `.github/workflows/ci-journal.yml` | 自身 |

不跑 `ruff format --check`：它对本仓 Python 本来就红，本 workflow 不拥有那份债（同 `ci-scripts.yml` 的记法）。

## 6. 回填 S16/17/18 的 `### Git Commits`

**口径是量出来的，不是猜的**：把 journal-1.md 里**已有表格的 25 个 session**逐个对照
`git log -S`（找出「记录该 session 的提交」）⇒ **25/25 都不把自己那条记录提交列进本表**，
表内是**在该提交之前落地的交付提交**。机制解释：表是在记录 session 的那一刻写的，
写不进还不存在的东西。

| session | 记录它的提交（不列入） | 回填条目 |
|---|---|---|
| S16（T14 #659） | `f28bf24` | `ca67b20`（T14 实现，`Tracker: #659`） |
| S17（T14 收口 / #664 / D15） | `5ea38c9` | `0c2604d`（#664 修复，`Tracker: #664`） |
| S18（T15 #660） | `d942fa9` | `dd3d2b3`（T15 实现，`Tracker: #660`） |

每条附一行回溯来源，并**点名未列入的那些提交**（`437265d` / `45e60b7` / `3d876df`）
⇒ 映射完整、可审计，没有东西被静默丢掉。

## 7. 验收（可逐条核对）

1. `python scripts/check-journal-ledger.py` → **exit 0**，`this ledger: 2 file(s), 30 session(s), 0 placeholder line(s), 30/30 session(s) conforming`；
   `legacy (pinned, not fixed here): 3 file(s), 123 session(s), 363 placeholder line(s)`。
2. 两份 journal 的 `## Session` 标题：30 个唯一编号，**每个 session 6 个标准标题各恰好一次**（回填前 S16/17/18 缺 `### Git Commits`）。
3. 三类占位符（外加 `(Add summary)`）**整行相等计数 = 0**；子串计数 > 0（同一份字节，证明判据不可放松）。
4. `pytest tests/python/test_check_journal_ledger.py -o addopts=""` → **27 passed**。
5. 消融自检：**9 条非等价突变全部打红**，集合两两不同；1 条**等价突变**（`strip(" ")`）带理由记录为预期绿；守卫按字节复原、sha256 前后一致。
6. `ruff check` 两个新文件 **All checks passed**；两文件 `ast.parse(feature_version=(3,9))` 通过。
7. `journal-1.md` 的回填是**小改动**（`--numstat` = 24/0，3 处 × 8 行），行尾仍统一 CRLF、孤立 LF = 0。
8. CI：Journal CI 绿（新），Python CI / Scripts CI 不受影响。

## 8. 非目标

- 不清 `.trellis/workspace/JameryW/` 那本账（363 处占位符 / 3 处重复编号）—— 不在 #676 范围内，见 §7「未处置」。
- 不改任何测试或产品代码；不重跑历史门禁。
- 不回填 S16/17/18 之外的任何正文。
- 不把 `- None - task complete` 之外的正文风格问题纳入门禁。

## 9. 已知限制 / 未处置

- **另一本账未清理**：`.trellis/workspace/JameryW/` 的 363 处占位符与 3 处重复 session 编号
  （49 ×2、74 ×2、98 ×3）被 pin 住但未处置 —— 建议另开票；本票只保证它**不会变坏**。
  它的 `index.md` 还写着 `Total Sessions: 119` 而语料里有 123 个 session 标题 ⇒ 已作为 ADVISORY 报出。
- **判据的代价**：整行相等意味着「讨论占位符」的正文必须加前缀。这条规则是门禁可持久的前提，
  已写进 docstring 与违规信息。
- **`journal-2.md` 的 index.md 行数是写入时刻的快照**（`~257` vs 实测），手工编辑后不会重算 —— 显示层近似，不是欠账。
- 塔尖：守卫**不**校验行尾统一（只作 ADVISORY），因为它取决于 `core.autocrlf` 而会在 CI/本机给出不同读数。
