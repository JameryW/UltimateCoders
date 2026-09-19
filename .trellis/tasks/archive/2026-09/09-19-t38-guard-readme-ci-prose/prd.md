# T38 PRD —— 把 README CI 段里**剩下的手抄陈述**接进对账守卫

- **票**：T38 ／ issue **#688**
- **承接**：T37 / #687 的 **账 3**；沿用 T37 立起的判据「**手抄的机器可读面必须有守卫**」与 T35 / #685 的「封闭/开放输入集」
- **依赖**：**不依赖**外部「方案第 21 节」原文（框架卫生线）
- **起点一手复核**（不继承上一轮叙述）：`HEAD = origin/main = 303e019`、工作树干净；
  开放 issue = **1**（#656，末次更新 `2026-09-18T13:45Z`，**无新内容**）；issue 最大号 687 ⇒ 本票 **#688**；
  归档最大 `-t37-` ⇒ 本票 **T38**。

---

## 一、缺口（一手读数，HEAD `303e019`）—— 三处都是**实测**，不是推演

| # | 陈述 | 原位 | 测法（沙箱副本，真仓未触碰） | 读数 |
|---|---|---|---|---|
| H1 | 段首**计数词**「Nine independent workflows」 | `README.md` § CI 首段 | `Nine` → `Eight` | 守卫 **`rc 0` 照绿** |
| H2 | 段首**计数词**「九套独立工作流」 | `README.zh-CN.md` § CI 首段 | 「九」→「八」 | 守卫 **`rc 0` 照绿**（同一条判据的两个面） |
| H3 | 脚注「Every path-filtered workflow's own YAML file also matches its `paths`」 | 同上，表后那段 | 把 `ci-dashboard.yml` 的**自身条目**从它**自己的** `paths` 删掉 | 守卫 **`rc 0` 照绿**，而该句此时**已为假** |
| H4 | 脚注后半「every workflow supports manual dispatch」 | 同上 | 实测 9 套全含 `workflow_dispatch` | **9/9 为真**，但**没有任何东西检查它** |

复算命令（H1/H2/H3 同一形状；下面这条是 H3，已跑过）：

```
# 沙箱 = 拷贝 .github/workflows/*.yml + 两版 README + 守卫到临时目录
# 然后从 ci-dashboard.yml 的 push 与 pull_request 两侧各删一行：
#       - ".github/workflows/ci-dashboard.yml"
# 实测：老守卫 rc 0，打印 “9 workflow(s) reconciled in 2 file(s) / check passed.”
```

### H3 是本票最值钱的一条

判据 4/5 的对账口径是：

```python
truth = set(push_paths) - {f".github/workflows/{wf}"}
```

**收下一个不存在的元素是空操作。** 也就是说，那条减法**默默容忍**它所依赖的前提失效 ——
而前提只写在**注释**里（“each workflow's own YAML file IS in its `paths` but is covered by a
footnoted sentence”），**从未被检查**。

⇒ 这正是 T37 自己立起的判据**没有被应用到它自己的脚注上**：一处**手抄的散文声明**，
靠的是一个**没被断言的假设**。

---

## 二、结论

| # | 结论 |
|---|---|
| A | 守卫的口径**不能建立在未检查的假设上** ⇒ 把「每个带过滤 workflow 自身命中自己的 `paths`」从**假设**变成 **J-self 判据**（这也让那条减法**有据可依**） |
| B | 段首**计数词**是手抄的**数字**，与仓库状态无关地漂移 ⇒ 必须对账（**J-count**）；且**找不到计数词必须红** —— 否则下次改措辞就**静默失效**，判据自己必须先会红 |
| C | 脚注后半是对 **9/9 的集合断言** ⇒ 可对账（**J-dispatch**） |
| D | 三处都在**同一输入面**内（两份 README + `.github/workflows/` 一个目录）⇒ **不新增输入面、不改任何 workflow、不改既有守卫的 7 条判据** |
| E | 本票的取证形状 = **同一组突变、同一仓库、只换守卫版本：旧守卫全绿、新守卫必红**（T34 式**两方向**）。这比「新守卫能红」强：它证明**补上了真实缺口**，而不是「新写了一段恰好会红的代码」 |
| F | 本票会移动被钉死的语料计数：`implement.jsonl` 进语料 ⇒ `check-tasks-refs` +1，钉值 `(792,793)` → **`(793,794)`**，同 change 更新 |

---

## 三、设计取舍（逐条写明为什么不选另一条）

| 选项 | 判读 |
|---|---|
| **A 在既有守卫里加判据（本票）** | **采纳**：同一输入面、同一权威源；且 `ci-readme-ci-table.yml` 的 `paths` **已含**该脚本与其测试 ⇒ **零接线成本** |
| B 新建第二个守卫文件 | **不采纳**：会把「读两版 README + 读 workflow YAML」的解析器**复制一份**（第二写者），而两份会各自漂移 |
| C 计数词只认阿拉伯数字 | **不采纳**：两版 README 现在都用**词**（Nine / 九套）⇒ 只认数字等于**现在就不匹配** ⇒ 只能选「改 README」或「报错」，两条都在**改被校验方来迁就校验器** |
| D 计数词词表无上限（贪心解析所有数词） | **不采纳**：会把散文里别的数字吃进来。词表**有界 1..12**，且**解析不到即红**（非空性） |
| E J-self 只认「恰好列了 `.github/workflows/<wf>`」 | **不采纳**（**实测反例**）：`ci-readme-ci-table.yml` 是通过 **`.github/workflows/**`** 覆盖自身的 ⇒ 只认具名会把**合法形态**打成红。但也不能全实现 glob 语义 ⇒ 采纳**最小匹配器：精确相等 或 `prefix/**` 前缀** |
| F J-self 的最小匹配器不带自检 | **不采纳**：手写匹配器正是 T37 警告的「静默返回空」形状 ⇒ 必须附**真理表**，且**正负对照都要**（T33：阳性对照自己也要消融） |
| G 一并校验 `Checks` 列的散文（「3 feature combos」「pytest（Python 3.9 + 3.12）」） | **不采纳（本票）**：那一列是**自由文本**，钉它 = 把散文句式变成契约。这需要先裁「是否接受」，属**另一票** ⇒ 记账 5，**不夹带** |

---

## 四、新增的三条判据面（**每条都要有自己的失败消息**，以便消融逐条钉住）

| # | 判据 | 失败消息（可定位） |
|---|---|---|
| 8 | **J-count**：段首**计数词** == `len(on_disk)` | `README.md: prose says Eight workflow(s), but 9 exist` |
| 8b | **J-count 非空性**：**必须解析出**一个计数词 | `README.md: no workflow count found in the CI prose`（防「改措辞即静默失效」） |
| 9 | **J-self**：每个**带过滤**的 workflow，其 `paths` 必须**覆盖自身 YAML**（具名 或 `prefix/**`） | `ci-dashboard.yml: paths no longer covers its own workflow file; the README footnote claims it does` |
| 10 | **J-dispatch**：每个 workflow 必须含 `workflow_dispatch` | `ci-x.yml: no workflow_dispatch trigger, but the README says all workflows support manual dispatch` |

⚠️ **J-count 的解析与文案解耦**：断言的是「**解析到的那个词** == 真值」，
所以下次把计数词从 `Nine` 改成 `9` 只要**词表覆盖**就仍然过；**不可解析**才红 —— 这条正是上面选项 C 的落点。

---

## 五、变更（**零 workflow 变更**）

1. `scripts/check-readme-ci-table.py`：加三条判据 + 一个**最小路径匹配器**（精确相等 / `prefix/**`）。
   同时**更新口径注释**：把那句「自身文件在 YAML 的 `paths` 里」（原本是**假设**）改写为
   「由 J-self 保证的**前置条件**」—— 一处陈述被代码追上，**同 change 改掉**（推论 A）。
2. `tests/python/test_check_readme_ci_table.py`：
   - `JUDGMENTS` 加三项（**既有覆盖断言会自动要求新突变到达它们** —— 这正是 T37 留下的机制）；
   - 加四处突变 **I**（EN 计数）/ **J**（ZH 计数）/ **K**（删自身条目）/ **L**（删 `workflow_dispatch`）；
   - 加一个**匹配器真理表**测试：正例（具名命中、`prefix/**` 命中、嵌套路径命中）+ **负例**（前缀不越界、无关路径不命中）。
3. `tests/python/test_check_tasks_refs.py`：语料钉值 `(792,793)` → **`(793,794)`**。
4. `.github/workflows/ci-readme-ci-table.yml`：**仅头注** —— 判据数 7 → 10、非目标段把「只对账表格」改为「表格 + 散文计数 + 脚注两句（`Checks` 列仍不校验）」。
   **`on:` / `jobs:` / `steps` 逐字不动**（既有 `paths` 已经覆盖该脚本与其测试）。

---

## 六、消融（**两路；缺一不算**）

### 方向一 —— **两方向**（本票最强的一点）

四处突变 **I/J/K/L**，各在沙箱副本上单独施加、按字节恢复：

| 突变 | 轴 | 旧守卫（T37 版，`303e019`） | 新守卫 |
|---|---|---|---|
| **I** `README.md` 的 `Nine` → `Eight` | J-count | **`rc 0` 绿** ← 缺口的一手证据 | `rc 1` |
| **J** `README.zh-CN.md` 的「九」→「八」 | J-count | **`rc 0` 绿** | `rc 1` |
| **K** 删 `ci-dashboard.yml` 的自身条目（push + pull_request 两侧） | J-self | **`rc 0` 绿** | `rc 1` |
| **L** 删 `ci-journal.yml` 的 `workflow_dispatch` | J-dispatch | **`rc 0` 绿** | `rc 1` |

⇒ 旧版**四处全绿**、新版**四处全红**：覆盖是真的（T34 判据）。

⚠️ **旧守卫的取得方式必须写进 PRD**，否则这份证据不可复算 —— 且**不能**放进测试文件：
`actions/checkout@v4` 默认 `fetch-depth: 1`（浅克隆）⇒ 测试里 `git show 303e019:…` 在 CI 上会失败。
取法：`git show 303e019:scripts/check-readme-ci-table.py > <沙箱>/scripts/check-readme-ci-table.py`。

⚠️ **`workflow_dispatch` 的删除**要注意锚点：`ci-journal.yml` 里该词可能出现在**注释**中
（T37 的突变 E 正是栽在这里）⇒ 锚点必须带缩进、且**声明期望出现次数**。

### 方向二 —— 匹配器真理表（正负对照）

| 模式 | 路径 | 期望 |
|---|---|---|
| `.github/workflows/ci-dashboard.yml` | 同名路径 | ✅ 命中 |
| `.github/workflows/**` | `.github/workflows/ci-readme-ci-table.yml` | ✅ 命中 |
| `crates/**` | `crates/uc-python/src/lib.rs` | ✅ 命中（嵌套） |
| `crates/**` | `cratesfoo/x` | ❌ **前缀不越界** |
| `docs/agents/*.md` | `docs/agents/domain.md` | ✅ 命中（`*` 段内） |
| `dashboard/**` | `docs/dashboard/x` | ❌ 不命中 |

⚠️ **负例与正例一样必须存在** —— 一个「永远返回 True」的匹配器会让 J-self **永绿**（装饰）。

---

## 七、验收（逐条可复算）

| # | 验收 |
|---|---|
| 1 | 新守卫对当前真仓 `rc 0`，且仍报告 **9** workflow / 2 file（既有 7 条判据不回归） |
| 2 | 三条新判据**各有独立消息**，且**每条至少被一处突变单独打到**（由既有覆盖断言**强制**，不是人数出来的） |
| 3 | **两方向**：旧守卫（`303e019` 版）对 I/J/K/L **四处全绿**；新守卫对同一四处**全红** |
| 4 | 匹配器真理表**正负例都通过**（不是「永远 True」） |
| 5 | 四个既有守卫 `rc 0`；`ruff check scripts/` 与 `ruff check python/ tests/` 通过 |
| 6 | 语料钉值 `(793,794)` 的**两个可达态都实测**；`test_check_tasks_refs.py` 通过；Python 总收集数 = **1214 + 本票新增用例数** |
| 7 | **不动任何 workflow 的 `paths`/`steps`**；`.github/workflows/**` 的唯一改动是
    `ci-readme-ci-table.yml` 的**头注**（判据 7→10、非目标段）—— 那是**会被代码超越的陈述**，
    按推论 A 必须同 change 修，故本行原写的「零 workflow 变更」在实现时**被自己追上**并已改 |
| 8 | CI：`ci-readme-ci-table.yml` **被触发**且两腿 success；其余 workflow 判词与基线一致 |

---

## 八、非目标与账（未静默丢弃）

- **非目标 1**：**不**校验 `Checks` 列的**散文**（「3 feature combos」「pytest（Python 3.9 + 3.12）」）——
  那是自由文本，钉它 = 把散文句式变成契约，需先裁「是否接受」。见**账 5**。
- **非目标 2**：**不**把 README 变成权威来源；本票继续以 **workflow 文件**为权威，README 是**被校验方**。
- **非目标 3**：**不**动 `check-spec-refs.py` / `check-tasks-refs.py` / `check-journal-ledger.py` 的判据。
- **账 1（延续 T36/T37）**：`paths` ↔ `Cargo.toml` **闭包**一致性仍无守卫（需 CI 有 Rust 工具链 + 先裁对账口径；仓内无法坐实）。
- **账 2（延续，本票实测更新口径）**：`ruff format --check` **实测 `scripts/` + `python/` + `tests/` 共 76 个文件待改**
  ⇒ 接线是**大票**、不是顺手事。`.trellis/.template-hashes.json` 无 job 校验；
  `.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- **账 3（本票部分偿还）**：T37 的账 3 是「README 里其他手抄机器可读面仍无守卫」。本票偿还**计数词 + 两条脚注**，
  但 `Checks` 列的散文数字**仍未偿还** ⇒ 转记**账 5**（**不是结清**）。
- **账 4（延续）**：`add_session.py` 写进个人 `index.md` 的 journal 行数取自**骨架追加后、正文填充前**
  ⇒ 当轮偏低（下次运行对所有文件重算 ⇒ 自愈）。⚠️ `journal-2.md` 已**恰好 2000 行** ⇒ 本票的 journal 会话
  应落 **`journal-3.md`**（脚本按运行时行数自动轮转）。
- **账 5（本票新增）**：`Checks` 列的散文数字（feature 组数、Python 版本）仍无守卫 —— 是否把散文句式变成契约：**待决**。
