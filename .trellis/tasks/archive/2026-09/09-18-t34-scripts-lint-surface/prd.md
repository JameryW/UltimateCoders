# T34 — 让 `scripts/**` 成为一等 lint 面（修掉两处既有 ruff 错误，把「只能点名文件」的临时形状退休）

## 元信息

- 票号：**T34** / issue **#684**
- 日期：2026-09-18
- 前置：T33（`82ab716` 实现 / `b895a7d` 归档 / `dcaef15` journal）；起点 `HEAD = origin/main = dcaef15`，工作树干净
- 交付面：`scripts/check-codex-issue-flow.py`、`.github/workflows/ci-scripts.yml`、`README.md`、`README.zh-CN.md`、
  `tests/python/test_check_tasks_refs.py`（语料钉值同 change 更新）
- 不交付：`.trellis/scripts/**`、`.claude/hooks/**`、`ruff format`

## 背景

T26（#675 切片 C）新建 `.github/workflows/ci-scripts.yml` 时一手测得两件事，并写进了文件头：

```
#   * `ruff` is scoped to the two named files, NOT to `scripts/`.  `ruff check
#     scripts/` is red today on pre-existing issues in check-codex-issue-flow.py
#     (I001 + UP045, last touched in fc9b5ce) and this workflow owns neither.
```

于是那个 workflow 的 ruff 步骤**只能点名文件**。T26 的 `prd.md:113` 把修这两处明列为**非目标**（「不在本票」）。

本票的起点是问 T26 没问的那个问题：**「点名文件」这个形状本身的代价是什么？**

## 侦察结论（全部一手，逐条可复算）

### 结论 A：代价是「**新加到 `scripts/` 的文件天生无人 lint**」

一手测量（`git ls-files '*.py'` ∩ 各 workflow 里 `ruff check` 的实参）：

| 目录 | tracked `.py` | 被某个 CI ruff target 覆盖 | 未覆盖 |
|---|---|---|---|
| `python/` | 36 | 36（`ci-python.yml:32` 目录级） | 0 |
| `tests/` | 51 | 51（同上） | 0 |
| `scripts/` | **5** | **4**（3 个在 `ci-scripts.yml:67/93-95`、1 个在 `ci-journal.yml:69`） | **1** |
| `.trellis/scripts/` | 28 | 1（`ci-trellis.yml:66` 的 `common/active_task.py`） | 27 |
| `.claude/hooks/` | 3 | 0 | 3 |
| **合计** | **123** | **92** | **31** |

`scripts/` 上唯一逃逸的是 `scripts/check-codex-issue-flow.py` —— 正是那两处 ruff 错误所在。
**而形状问题比这两行错误大**：只要 ruff 步骤仍点名文件，**下一个**加进 `scripts/` 的守卫（T30 那种）就会同样天生无人 lint。
本票要消的是这个**类**，不是那两行。

### 结论 B：这两处错误是**真欠账**，不是已决冻结

判据（本仓已在用的口径）：「已决冻结」会在输出里自认（`[legacy]` / `pinned, not fixed here`）。实测：

| 出处 | 逐字 | 判读 |
|---|---|---|
| T26 `prd.md:113` | `**不**修 ... 的两处既有 ruff 问题（不在本票）。` | 列在 **非目标** —— 「本票不做」 |
| T26 `prd.md` 的 `## 遗留（不在本票）` | 列了 2 项（已删子系统的 spec 正文、`SUBJECT_REMOVED` 计数） | **不含**这两处 ⇒ 既非遗留、亦非冻结 |
| `ci-scripts.yml:21` | `and this workflow owns neither` | 「**没人**拥有」，不是「决定永不修」 |

⇒ 一笔**无人认领的既有欠账**。

### 结论 C：修完它，文件头那句注释会**变成假话** —— 必须同票修掉

`ci-scripts.yml:20` 逐字宣称 `` `ruff check scripts/` is red today ``。本票修完它就不再成立。
仓规「过期规格比没有规格更危险（自带权威感）」⇒ 注释与代码不一致就**同票**修掉。

### 结论 D：`ruff format` **仍不接**（沿用 T26 的裁决，理由未变）

T26 一手测过：`ruff format --check` 改动前后都失败（用 `git show HEAD:` 取证）⇒ 接上会引入上百行无关重排。

### 结论 E：`paths` 是 **workflow 级**的，所以「正确过滤的纯 lint workflow」在本仓无处安放

| 形状 | 输入集 | 带 `paths` 是否正确 |
|---|---|---|
| `spec-refs` / `tasks-refs`（现役） | 守卫读**全仓**（索引） | ❌ 过滤会**恰好对翻判词的那类改动瞎** |
| 本票的 `ruff check scripts/` | **恰好** `scripts/**`（封闭） | ✅ 语义正确 |

但 `paths:` 在 Actions 里是 **workflow 级、不是 job 级** ⇒ 无法在同一文件里同时放「无过滤的守卫 job」与「带过滤的 lint job」。
两个选项：**(i)** 放进既有的 `ci-scripts.yml`（无 `paths`，每次 push 都跑，代价 = 一次 `pip install ruff` + 一次 lint；
该文件的设计注释本来就写明「刻意做得很小，好让每次 push 跑得起」）；**(ii)** 新开第 8 个 workflow 只为带 `paths: scripts/**`。
取 **(i)**：仓规「接新守卫一律独立小 job，**别动既有 workflow 的 `paths`**」，且 (ii) 只为省下每次 push 的一次 lint，不值得多一个 workflow。
**代价（有意）**：`scripts/**` 的改动从此会触发这个 job —— 它本来就该触发。

### 结论 F：新 job 与既有两个 job 的 ruff 面**有意的重叠**

`ruff check scripts/` 会把 `check-spec-refs.py` / `check-tasks-refs.py` / `check-tasks-refs-selftest.py` 再查一遍。
**保留重叠**：既有两个 job 的 ruff 步骤是它们**自身契约**的一部分（它们同时跑守卫与其测试），删掉会让「守卫 job 绿」依赖另一个语义单元里的步骤。重叠面 ≤ 3 个文件。
⇒ 因此本票**不改**既有 job 的任何一行。

### 结论 G：仍未被覆盖的两个面，都已有**登记在案的排除理由**（非目标）

| 面 | 数 | 登记出处 | 理由 |
|---|---|---|---|
| `.trellis/scripts/**` | 27 | `ci-trellis.yml:20-31` | 框架代码／需与上游模板保持可合并；实测 `task.py` 8 处、`common/task_utils.py` 6 处既有 findings |
| `.claude/hooks/**` | 3 | `.trellis/.template-hashes.json:8-10` | 与 `.trellis/scripts/**` 同属模板面（三个 hook 的 sha256 都在登记表里） |

⇒ **已登记的排除**，不是欠账。本票只在结论 A 的表里**记账**，不动它们。

### 结论 H：`scripts/check-codex-issue-flow.py` 是一个**无武装**的守卫（非目标，但必须记账）

它校验的是**仓内已跟踪**的输入（`.agents/skills/**` 14 个目录的 `SKILL.md` 与 `name:`、`docs/agents/*.md`、
`docs/workflows/codex-issue-flow.md`、`AGENTS.md` 指针），自述 "without network access" ⇒ **CI-safe**。但：

- **今天 `rc=0`**（`Codex issue workflow validation passed.`）；
- **零个 workflow 运行它** —— 4 个 workflow 里唯一出现它文件名的是 `ci-scripts.yml:20` 的**注释**；
- 唯一的使用说明在 `.agents/skills/ultimatecoders-issue-flow/SKILL.md:19`：人工在「setup looks incomplete」时跑。

⇒ 它的漂移对**每一个门禁**都不可见。这是真欠账，但属**引入一个新行为门禁的决定**，另票。

## 变更（Scope）

1. **修 `scripts/check-codex-issue-flow.py` 的两处 ruff 错误**（形状由 `ruff check --fix --diff` 给出，非推测）：
   - `I001`：import 块与首个模块级语句之间的**两个空行 → 一个**；
   - `UP045`：`-> Optional[str]` → `-> str | None`，并删掉**随之变成未使用**的 `from typing import Optional`。
     ⚠️ 安全性一手核对：`Optional` 全文件**恰好 2 次**（第 7 行 import、第 38 行标注）；第 3 行有
     `from __future__ import annotations` ⇒ 标注延迟求值，3.9 下 `str | None` 合法（T26 结论 D 的同一口径，本票用 `ast.parse` 复验）。
   - 实测 **3772 → 3738 B**（−29 import 行 −2 空行 −3 标注），CRLF 104 → 102，**行为不变**（`rc 0`、输出逐字同）。
2. **`.github/workflows/ci-scripts.yml` 新增第三个 job `scripts-lint`**：`ruff check scripts/`（`python-version: "3.12"`）。
   **不改**既有两个 job 的任何一行（结论 F），**不动** workflow 级 `paths`（该文件本来就没有）。
3. **同票修掉文件头那段已变假话的注释**（结论 C），并把「为什么这里是**目录级**、而另两处**点名文件**」与
   「为什么这个 job 在本文件而不是另开 workflow（`paths` 是 workflow 级）」的理由写进去（结论 A/E）。
4. **`README.md` / `README.zh-CN.md` 的 Lint 配方**：`ruff check python/ tests/` → `ruff check python/ tests/ scripts/`。
   配方漏了 `scripts/`，下一个加脚本的人就不会 lint 它 —— 正是本票要消的那个类。
5. **`tests/python/test_check_tasks_refs.py` 语料钉值同 change 更新**（本票 `implement.jsonl` 进语料；按该钉 docstring 自己的要求）。

## 消融（实测，两个方向都做）

### 方向一：新形状**能红**（umbrella job 的阳性对照）

| 突变 | `ruff check scripts/` 实测 |
|---|---|
| M0 基线（未突变） | `rc=0` **GREEN** `All checks passed!` |
| M1 `scripts/` 下**新增**一个含 `F401` 的文件 | `rc=1` **RED** `F401 [*] `os` imported but unused` |
| M2 还原 `UP045`（写回 `Optional[str]` + import） | `rc=1` **RED** `UP045 Use `X | None` for type annotations` |
| M3 还原 `I001`（多插一个空行） | `rc=1` **RED** `I001 [*] Import block is un-sorted or un-formatted` |

### 方向二：旧形状对同一突发**是瞎的**（本票的**判据性**消融）

同一份 M1（`scripts/` 下一个含 `F401` 的**新**文件）下，逐字跑 CI 今天实际在跑的三条命名文件命令：

| 命令（逐字取自 workflow） | 实测 |
|---|---|
| `ci-scripts.yml:67` → `check-spec-refs.py` + 其测试 | `rc=0` **GREEN** |
| `ci-scripts.yml:93-95` → `check-tasks-refs.py` + selftest + 其测试 | `rc=0` **GREEN** |
| `ci-journal.yml:69` → `check-journal-ledger.py` + 其测试 | `rc=0` **GREEN** |
| **本票的形状** `ruff check scripts/` | `rc=1` **RED** |

⇒ 三条旧命令**同时绿**而新形状**红**，判词**不同** ⇒ 本票**确实增加了覆盖**，不是装饰。
（反向的装饰判据由 T19 给出：两处突变打红同一集合 ⇒ 其中一条是装饰。）

突变全部按字节恢复，并由**独立进程**从磁盘复算 sha256 认证：`015a4feafee8455db579c4eef6fa708a19ff7d4f4219f160d751847cae7147fc`。

## 验收（实测）

| # | 项 | 结果 |
|---|---|---|
| 1 | `ruff check scripts/` | `Found 2 errors` → **`All checks passed!`**（rc 0） |
| 2 | 守卫行为不变 | `python scripts/check-codex-issue-flow.py` ⇒ `Codex issue workflow validation passed.`，**rc 0**（改动前后同） |
| 3 | 3.9 语法 | `ast.parse(src, feature_version=(3,9))` 通过 |
| 4 | 消融两方向 | **PASS**（见上两表） |
| 5 | workflow | `yaml.safe_load` 通过；job 数 **2 → 3**；`spec-refs`/`tasks-refs` 的 `steps` 与 HEAD **逐字相同**（6 步 / 7 步）；workflow 无 `paths` |
| 6 | 文件头注释 | 不再包含 `is red today` |
| 7 | 两个 README | 含 `scripts/`；行尾仍 **CRLF-only**；各 +9 B（`--numstat` 1/1） |
| 8 | 语料钉值 | `(788,789)` → **`(789,790)`**（实测），`0 dangling / 0 malformed` 不变 |
| 9 | Python 收集总数 | **1211 不变**（本票不增删测试） |
| 10 | CI | 推送后四个 job 全绿，且 `scripts-lint` **实测被触发** |

## 非目标

- **不**接 `ruff format`（结论 D）。
- **不**动 `.trellis/scripts/**`（27 处）与 `.claude/hooks/**`（3 处）—— 已登记排除（结论 G）。
- **不**把 `scripts/check-codex-issue-flow.py` 接进 CI（结论 H：那是引入新门禁的决定，另票）。
- **不**改既有两个 job 的 ruff 步骤（结论 F）。
- **不**改任何 workflow 的 `paths`。

## 遗留（不在本票）

- **`scripts/check-codex-issue-flow.py` 无武装**（结论 H）：CI-safe 的 wiring 守卫零个 workflow 运行它。应另开票决定是否接进门禁。
- `.trellis/scripts/**` 27 处 / `.claude/hooks/**` 3 处 ruff findings：已登记排除（结论 G）。
- `ruff format --check` 仍整体失败（T26 起就是如此）。
