# T35 PRD —— 把 `check-codex-issue-flow.py` 接进门禁（仓内唯一「无武装」的守卫）

- **票**：T35 ／ issue **#685**
- **承接**：T34 / #684 的账本条目「`scripts/check-codex-issue-flow.py` 是**无武装**的 CI-safe 守卫（无 workflow 跑它）⇒ 是否接进门禁**需另开票**」
- **依赖**：**不依赖**外部「方案第 21 节」原文（框架卫生线）
- **起点一手复核**（不继承上一轮叙述）：`HEAD = origin/main = 8cae2fe`、工作树干净；
  开放 issue = **1**（#656）；归档最大 `-t34-` ⇒ 本票为 **T35**；`gh issue list` 最大号 684 ⇒ 本票 **#685**。

---

## 结论

| # | 结论 |
|---|---|
| A | `check-codex-issue-flow.py` 守 **21 条被断言路径**（对外可见面 **50** 个 tracked 文件），**被 0 个 workflow 运行** ⇒ 21 条断言 = 宣称的能力、零执行 |
| B | 「无 workflow 提到 `.agents/`/`AGENTS.md`」+「唯一索引含该面的 `check-spec-refs.py` 是劝告式、从不失败」⇒ 现有**全部门禁**对该面判词**恒绿** |
| C | 该守卫的**输入集是封闭的**（固定 21 条路径、**不 glob 目录**）⇒ 给它加 `paths` 过滤是**正确的**；这与 `spec-refs`/`tasks-refs` 的「读开放输入集、故必须不过滤」**方向相反** |
| D | `paths` 是 **workflow 级**（T34 已记）⇒ 一个**被过滤**的 job **不能**与必须保持不过滤的 `ci-scripts.yml` 同文件 ⇒ 本票**新建独立 workflow**，且**不动任何既有 workflow 的 `paths`/`steps`** |
| E | 被守面里 `.agents/skills/**`(51) 与 `AGENTS.md`(1) **在** `.trellis/.template-hashes.json` 里（模板同步面）；`docs/agents/*.md`(4) 与 `docs/workflows/codex-issue-flow.md` **不在** ⇒ 后者**既无门禁也无模板登记**；本票只**接入**，**不**重构模板面内容 |
| F | 两方向消融都通过（见 §三）：6 处结构突变各自变红且集合**两两不相交**；同一真仓突变下 **5/5 既有命令判词不变** ⇒ 覆盖是真的，不是重复 |
| G | 本票会**移动被钉死的语料计数**：新票 `implement.jsonl` 自引 `prd.md`（唯一 `.trellis` 前缀引用）⇒ 钉值 `(789,790)` → **`(790,791)`**，同 change 更新 |
| H | 同上，顺手修掉测试 docstring **首行已过期**的自述（仍写 `as of T33 / #683: 788`，而其 T34 小节已写到 790）—— 同段落内自相矛盾，属「过期陈述自带权威感」 |

---

## 一、缺口的一手读数（HEAD `8cae2fe`）

| 检查 | 命令 | 读数 |
|---|---|---|
| 谁在 `.github/workflows/` 里提到该守卫 | `git grep -l check-codex-issue-flow.py HEAD -- .github/workflows/` | 仅 `ci-scripts.yml`，且**只在文件头散文**（讲它那两处 ruff 错误）——**无 job 运行它** |
| 谁提到被守面 | `git grep -l -e '\.agents/' -e 'AGENTS.md' HEAD -- .github/workflows/` | **0 处** |
| 唯一索引含该面的守卫 | `git grep -n os.walk HEAD -- scripts/check-spec-refs.py` | `:327` `os.walk(ROOT)` ⇒ 索引含 `.agents/**`；但该守卫的 `--audit` **设计上从不失败**（`ci-scripts.yml:85-86` 自述）⇒ 判词恒绿 |
| 模板登记 | `.trellis/.template-hashes.json`（323 条） | `.agents/skills` **51**、`AGENTS.md` **1**；`docs/agents/*.md` ×4 与 `docs/workflows/codex-issue-flow.md` **均不在** |
| 接进 CI 是否生来就红 | 逐条比对 21 条被断言路径 vs `git ls-files` | **21/21 全部 tracked** ⇒ 干净 checkout 即满足，**无生来红风险** |

**其他 4 个守卫都有 job**（`check-spec-refs.py` → `ci-scripts.yml:spec-refs`；`check-tasks-refs.py` 与
`-selftest.py` → `:tasks-refs`；`check-journal-ledger.py` → `ci-journal.yml:journal-ledger`）
⇒ **只有这一个没有**。这也解释了为什么 T34 只能把它写进账本而不能顺手接上。

---

## 二、为什么是「新建独立 workflow + 封闭输入集过滤」

1. **过滤是对的**：该守卫遍历 `REQUIRED_SKILLS`（14 项，硬编码 dict）与 `REQUIRED_FILES`（6 项硬编码），
   **从不 glob 目录** ⇒ 能翻转它判词的改动**恰好**是这 21 条路径本身（+ 守卫文件 + workflow 文件）。
   这是「封闭输入集」，与 `spec-refs`（`os.walk` 全仓）、`tasks-refs`（任意归档目录都可翻转）**不同族**。
2. **过滤必须是 workflow 级** ⇒ 不能放进 `ci-scripts.yml`（其两 job 读开放输入集，必须保持不过滤）。
   若把本 job 塞进那里并保持不过滤，也不是错，但会让该文件的**归属**变模糊（它自称守 `scripts/`），
   且每次 push 都跑一个与本次改动无关的守卫。**独立文件 + 正确过滤**更省、更自解释。
3. **零依赖**：守卫是纯 stdlib（`re` / `pathlib`），job 内**不需要 `pip install`** ⇒ 不会随依赖漂移。
4. **3.9/3.12 矩阵**：守卫靠 `from __future__ import annotations` 才能在 3.9 上写 `-> str | None`
   与 `list[str]`。矩阵把这条隐性要求**钉住**（同 `ci-scripts.yml` 对 `spec-refs` 的做法与理由）。

`paths` 条目（9 条，= 实测输入集 + 守卫自身 + workflow 自身）：

```
.agents/skills/**
AGENTS.md
docs/agents/issue-tracker.md
docs/agents/triage-labels.md
docs/agents/domain.md
docs/agents/mattpocock-skills.md
docs/workflows/codex-issue-flow.md
scripts/check-codex-issue-flow.py
.github/workflows/ci-codex-flow.yml
```

> 用**逐条精确路径**而非 `docs/**` 之类的宽 glob：守卫断言的正是这 4+1 个文件，
> 精确列出才能让「`paths` 就是输入集」这句话可被逐条核对。删除也会命中旧路径，故不丢信号。

---

## 三、判据性消融（两方向，缺一不算）

### 方向一 —— 守卫有牙（合成根，**仓未被触碰**）

在 `tempfile` 合成根里搭出与真仓同形的结构（14 技能 + 6 文件 + `openai.yaml`），复制守卫进去。
基线在**合成根与真仓都为绿**（`rc 0`，`Codex issue workflow validation passed.`）。六处结构突变，**一次一处**：

| 突变 | 期望失败行 | 实测 |
|---|---|---|
| M1 `AGENTS.md` 丢 `$ultimatecoders-issue-flow` 指针 | `AGENTS.md does not point to …` | rc 1 ✓ |
| M2 某技能 `name:` 漂移（`tdd` → `tdd-renamed`） | `skill name mismatch: …` | rc 1 ✓ |
| M3 入口技能插入 `[TODO` | `entry skill contains an unfinished TODO` | rc 1 ✓ |
| M4 `agents/openai.yaml` 去掉 `default_prompt:` | `entry skill metadata is missing a default prompt` | rc 1 ✓ |
| M5 入口技能丢掉 `$code-review` | `entry skill is missing reference: $code-review` | rc 1 ✓ |
| M6 删除 `docs/agents/domain.md` | `missing workflow file: docs/agents/domain.md` | rc 1 ✓ |

**失败集合两两不相交**（每条突变打红**唯一**一条断言）⇒ 没有哪条突变是装饰，
六个断言族各自独立被钉住（判据取自 `topics/t30-guard.md` 的「两处突变打红同一集合 ⇒ 一条是装饰」）。

### 方向二 —— 旧形状对同一突变是瞎的（真仓，judgment difference）

真仓 `AGENTS.md` 做一处突变（替换指针）。**CI 今天实际在跑的 5/5 命令判词全部不变（GREEN）**，
只有新接入的 `python scripts/check-codex-issue-flow.py` **翻红**：

| 命令（现有） | M0 未改动 | M1 突变 | 判词 |
|---|---|---|---|
| `ruff check scripts/check-spec-refs.py tests/python/test_check_spec_refs.py` | GREEN | GREEN | 不变 |
| `ruff check scripts/`（T34 新增的目录级） | GREEN | GREEN | 不变 |
| `python scripts/check-spec-refs.py --audit` | GREEN | GREEN | 不变 |
| `python scripts/check-tasks-refs.py --audit` | GREEN | GREEN | 不变 |
| `python scripts/check-journal-ledger.py --verbose` | GREEN | GREEN | 不变 |
| **`python scripts/check-codex-issue-flow.py`** | GREEN | **RED** | **翻转** |

只证「新命令会红」**不够**（T19 判据）—— 必须让旧形状在**同一突变**下**保持绿**，才排除「只是重复旧覆盖」。

**恢复口径（按字节 + 独立认证）**：`AGENTS.md` 的 `git ls-files --eol` 报 `i/lf w/crlf`
⇒ **blob 是 LF、工作树是 CRLF**，所以「与 blob 逐字节相等」**不是**正确的恢复判据；
改为**快照工作树字节 → 恢复该快照**，并由三重独立证据认证：
① 另一进程复算 sha256 = 快照值；② `git diff --exit-code HEAD -- AGENTS.md` `rc 0`；
③ `git status --porcelain` 为空。
> ⚠️ 这条是 T34 记下的同类坑的**镜像**：T34 是「blob 转 CRLF」，这里是「工作树是 CRLF、blob 是 LF」——
> **同一源（`core.autocrlf`），方向相反**。任何「与 blob 比对」的判据都必须先问行尾方向。

---

## 四、验收（逐条可复算）

| # | 验收 |
|---|---|
| 1 | `ci-codex-flow.yml` 通过 `yaml.safe_load`；job 数 **1**；`paths` 条目 **9**；step 数与设计一致 |
| 2 | 该 workflow **无 workflow 级以外的过滤**、且**不改任何既有 workflow**（`git diff --stat` 只含本票新增/说明面） |
| 3 | `python scripts/check-codex-issue-flow.py` rc 0，**Python 3.9 与 3.12 各一次** |
| 4 | `ruff check scripts/` 仍 `All checks passed!`（T34 的门禁不被本票打红） |
| 5 | 方向一：6 处突变各自变红、集合两两不相交、基线在合成根与真仓都绿 |
| 6 | 方向二：5/5 既有命令判词不变 + 新命令翻红；恢复由独立进程复算 sha256 认证 |
| 7 | `check-spec-refs` / `check-tasks-refs` / `check-journal-ledger` 均 rc 0 未动 |
| 8 | `test_check_tasks_refs.py` 通过；钉值 `(790,791)`；Python 总收集数 **不变** |
| 9 | `docs/workflows/codex-issue-flow.md` 加指针后仍被守卫认作 REQUIRED_FILE（rc 0 不变） |
| 10 | 推送后 CI：新 workflow success；Rust / Python / Scripts / Journal / Trellis 判词与基线一致 |

---

## 五、非目标与账（未静默丢弃）

- **非目标 1**：**不**改 `.agents/skills/**` 与 `AGENTS.md` 的内容。它们在
  `.trellis/.template-hashes.json` 里，属模板同步面 —— 对它们做「本地修好」会被同步覆盖，
  且会让本票的重心从「接入」漂成「重构」。本票只让**已有的断言**开始执行。
- **非目标 2**：**不**动 `ci-scripts.yml` 的 `paths`（无 `paths`）与两个 job 的 `steps`
  —— 既有 workflow 的触发面是 T34 已记的铁律面，本票只**新增**一个文件。
- **非目标 3**：**不**给 `.trellis/scripts/**`、`.claude/hooks/**` 等模板面加 ruff target
  （T34 已按登记排除，理由见 `ci-scripts.yml` 文件头）。
- **账 1**：`ruff format --check` 仍未接线（T34 已记；对守卫前后都失败 ⇒ 会引入无关重排）。
- **账 2**：本票**不**校验 `.trellis/.template-hashes.json` 与工作树是否一致。
  该清单被 `.trellis/scripts/common/safe_commit.py` **读取**（用于判定「哪些是模板文件」），
  但**无任何 job 校验其哈希**。这一面**是否该由 CI 校验**需要上游模板意图（同步时机/是否允许本地改），
  **仓内无法坐实** ⇒ 本票**不臆造**，**记账待决**。
- **账 3**：`docs/` 面除 spec 引用（`check-spec-refs.py`，劝告式）外无门禁；本票只覆盖
  「Codex issue workflow 的 wiring」这一子集，**不**声称覆盖 `docs/**` 全体。
