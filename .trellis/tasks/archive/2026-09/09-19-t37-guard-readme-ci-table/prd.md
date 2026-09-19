# T37 PRD —— 把 README 的 CI 触发面表与 workflow YAML 对账（**手抄副本必须有守卫**）

- **票**：T37 ／ issue **#687**
- **承接**：T36 / #686 的 **账 3**（本票就是那条账的落地），并沿用 T35 / #685 立起的「封闭/开放输入集」判据
- **依赖**：**不依赖**外部「方案第 21 节」原文（框架卫生线）
- **起点一手复核**（不继承上一轮叙述）：`HEAD = origin/main = 4a5f1cc`、工作树干净；
  开放 issue = **1**（#656，仅此一张）；issue 最大号 686 ⇒ 本票 **#687**；
  归档最大 `-t36-` ⇒ 本票 **T37**。

---

## 一、缺口（一手读数，HEAD `4a5f1cc`）

| 检查 | 命令 / 出处 | 读数 |
|---|---|---|
| 该表存在 | `README.md` § CI、`README.zh-CN.md` § CI | 两版各 **8 行**，每行 = 一个 workflow + 其触发路径 |
| 有没有人校验它 | `git grep -ln "README" -- scripts tests .github` | **0 处** —— 没有任何脚本或 workflow 读它 |
| 它会不会自己说话 | —— | **不会**：改 `paths` 不打红任何东西；改 README 也不打红任何东西 |
| 漂移史 | T36 重写时的实测 | 该段此前**漂移了 6 套 workflow**（写「2 套」而实有 8 套）**无人发现** |
| 手抄会失真（**已实证**） | T36 重写该表时 | 我为 `ci-codex-flow.yml` 写了 `` `docs/agents/*.md` `` —— **该 YAML 里没有这个 glob**（是 4 个具名文件）。看起来完全合理、格式正确、语气一致，**只靠阅读绝不会发现**；写成对账脚本后**当场变红** |
| 那次抓到它的东西 | —— | 一个**临时脚本**，留在被 ignore 的目录里，**未入库、无人运行** ⇒ 它抓到一次就作废了 |

⇒ 一处**会漂移的重复 + 零守卫**，而它的职责是告诉读者「改什么会触发什么」。
**错在这张表上 = 把触发面的知识污染给每一个人**（T36 里我正是照着错表去推「Rust CI 会跑」的）。

---

## 二、结论

| # | 结论 |
|---|---|
| A | 该表是 `on.push.paths` 的**手抄副本**，而「手抄」这一步**没有也不会**变可靠 ⇒ **必须**由机器对账，不能靠人读 |
| B | 守卫的输入集是**封闭**的：写死的两份 README + `.github/workflows/` 这一个目录 ⇒ **可以**做 `paths` 过滤（T35 判据） |
| C | `paths` 是 workflow 级 ⇒ 带过滤的 job **不能**放进必须保持无过滤的 `ci-scripts.yml` ⇒ **新建独立 workflow**，不动任何既有 `paths`/`steps` |
| D | **PyYAML，不手写 YAML 子集解析器**（取舍见下）：判据是「**永远不会红的守卫不是证据**」—— 手写解析器在格式变化时**静默返回空**，守卫变假绿，这是本票**最不能**犯的错 |
| E | 但**README 那一半只能**手写解析（表格是给人读的）⇒ 用**非空性断言**（必须看到 ≥N 行、≥N 个 workflow）防住「解析静默返回空」—— 这比选哪个解析器更要紧 |
| F | 本票**自带一次真实漂移**：新建第 9 套 workflow 后，README 表**必然**少一行 ⇒ 守卫**必然先红**，补行后转绿。**这不是合成突变，是真漂移**，比任何沙箱消融都强 |
| G | 顺带钉住另一条散文陈述：README 说 8 套「都指向 `main`」⇒ 守卫一并校验 `branches == ["main"]`（同样是从同一份 YAML 读，不新增输入面） |
| H | 本票会**移动被钉死的语料计数**：新票 `implement.jsonl` 进语料 ⇒ `check-tasks-refs` +1，钉值 `(791,792)` → **`(792,793)`**，同 change 更新 |

---

## 三、设计取舍（**逐条写明为什么不选另一条**）

| 选项 | 判读 |
|---|---|
| **A 用 PyYAML + `pip install pyyaml`（本票）** | **采纳**：`pyyaml` 已是本项目**运行时依赖**（`pyproject.toml:34`，`config.py`/`repo_config.py` 在用）⇒ 不是新增依赖；代价是一次 ~1s 的安装 |
| B 手写 YAML 子集解析器（零依赖） | **不采纳**：T35 的「零依赖」论据针对的是**文件存在性**守卫（纯 `re`/`pathlib`）。本守卫比较的是 **YAML 语义**，手写解析器在缩进/引号/内联列表变化时**静默返回空** ⇒ 守卫变**假绿**。这正好撞在「换掉被测输入后仍通过 ⇒ 什么都没测」上 |
| C 放进 `ci-scripts.yml` 当一个新 job | **不采纳**：`paths` 是 workflow 级，而 `ci-scripts.yml` 的 job **必须**保持无过滤（它读全仓）⇒ 同文件不可能 |
| D 不加守卫，只在 README 里写「以 YAML 为准」 | **不采纳**：T36 已经这么做了（加了那句话），**仍然**是我自己写错了 glob —— 一句话劝告**不是**判据 |
| E 把 README 表改成自动生成的块 | **不采纳**：README 要给人读，自动生成会引入一个新的写者 + 新的「谁跑它」问题；判据应落在**校验**上，不是**生成**上 |

---

## 四、守卫的判据面（**每条都要有自己的失败消息**，以便消融逐条钉住）

| # | 判据 | 失败消息（可定位） |
|---|---|---|
| 1 | 非空性：两版 README 各解析出 ≥ **1** 行，workflow 目录 ≥ **1** 个 | `README.md: parsed 0 CI rows`（防假绿的第一道门） |
| 2 | 存在性：表里的 workflow 集合 == 磁盘上 `*.yml` 集合 | `workflows on disk but not in table: [...]` / `table names workflows that do not exist: [...]` |
| 3 | 对称性：`push.paths` == `pull_request.paths` | `push/pull_request paths differ; one row cannot describe both` |
| 4 | 不虚构：声称集 ⊆ 真值集（**T36 抓到我的那条**） | `row claims paths the YAML does not have: [...]` |
| 5 | 不省略：真值集 ⊆ 声称集 | `YAML has paths the row omits: [...]` |
| 6 | 无过滤的 workflow 必须声称「空」，反之亦然 | `no paths filter, but the row lists [...]` / `has a paths filter, but the row claims none` |
| 7 | 散文陈述可钉：`branches == ["main"]`（push 与 pull_request 两侧） | `branches are [...], but the README says all workflows target main` |

判据 4/5 的对账口径：**声称集 == 该 YAML 的 `paths` 去掉自身 YAML 文件名**
（自身文件由 README 的一句脚注统一说明，不占表格格子）。

---

## 五、变更

1. **新增** `scripts/check-readme-ci-table.py`。
2. **新增** `.github/workflows/ci-readme-ci-table.yml`：单 job、矩阵 `["3.9","3.12"]`、
   `paths` = `README.md`、`README.zh-CN.md`、**`.github/workflows/**`**（后者覆盖「新增第 9 套」的情形）、
   `scripts/check-readme-ci-table.py`、`tests/python/test_check_readme_ci_table.py`。
   **不动任何既有 workflow 的 `paths` 或 `steps`。**
3. 两版 README 的 CI 段**各补第 9 行**（本票新建的 workflow）—— 顺序：**先让守卫变红，再补，再转绿**。
4. **新增** `tests/python/test_check_readme_ci_table.py`：真仓 pin（`rc 0`）+ **八处**单点突变
   （A–H，覆盖七条判据，并由断言强制「无一条判据落空」）。
5. `tests/python/test_check_tasks_refs.py`：语料钉值 `(791,792)` → **`(792,793)`**。

---

## 六、消融（**两路；缺一不算**）

### 方向一 —— **真实漂移**（本票独有，比合成强）

新建第 9 套 workflow 后，README 表**必然**少一行 ⇒ 守卫**必然红**，且红的正是「on disk but not in table」。
补上两版 README 的第 9 行 ⇒ 转绿。**这是真实的、必经的漂移，不是我编的突变。**

### 方向二 —— 八处合成突变（沙箱副本，真仓未触碰，逐处按字节恢复）

| 突变 | 轴 | 期望 |
|---|---|---|
| **A** 行内加假路径 | 判据 4（不虚构） | rc 1 |
| **B** 行内删真路径 | 判据 5（不省略） | rc 1 |
| **C** 表里改名不存在的 workflow | 判据 2（存在性） | rc 1 |
| **D** 只改 YAML 不改 README | **真实漂移场景** | rc 1 |
| **E** 只改 `push` 不改 `pull_request` | 判据 3（对称性） | rc 1 |
| **F** 把某 workflow 的 `branches` 改成非 `main` | 判据 7（散文陈述） | rc 1 |
| **G** 删掉某 workflow 的 `paths` 过滤块 | 判据 6（过滤形状） | rc 1 |
| **H** 把 `## CI` 标题改名 | 判据 1（非空性） | rc 1 |

⚠️ **两条方法论（T36 记下的，本票照做）**：
1. **「每条分支都被钉住」>「突变数够多」** —— 判据 5 会被 B 和 D 同时打到（同一条分支的两个漂移方向），
   所以**必须**补 E、F、G、H，否则判据 3/6/7/1 **没有任何突变到达**而照样有代码
   （判据 6 与 7 的分支在 A–D 里根本走不到）。
2. **一次只动一个轴** —— D 必须**同时**改 `push` 与 `pull_request`，否则打红的是判据 3 而不是判据 5。

---

## 七、验收（逐条可复算）

| # | 验收 |
|---|---|
| 1 | 守卫对当前真仓 `rc 0`，并明确报告校验了 **9** 个 workflow |
| 2 | **先红后绿**：新建 workflow 后守卫 `rc 1` 且消息为「on disk but not in table」；补两版 README 第 9 行后 `rc 0` |
| 3 | 八处合成突变 **8/8 变红**，且**七条判据每条至少被一处突变单独打到**
    （由 `test_every_judgment_is_pinned_by_a_mutation` 断言，不是靠人数出来的） |
| 4 | 四个既有守卫 `rc 0`；`ruff check scripts/` 与 `ruff check python/ tests/` 通过 |
| 5 | 语料钉值 `(792,793)`；`test_check_tasks_refs.py` 通过；Python 总收集数 = **1211 + 本票新增用例数** |
| 6 | **不动任何既有 workflow 的 `paths`/`steps`**（`git diff --stat` 只含本票文件） |
| 7 | CI：新 workflow **被触发**且 success（3.9 + 3.12 两腿）；其余 workflow 判词与基线一致 |

---

## 八、非目标与账（未静默丢弃）

- **非目标 1**：**不**校验 CI 段的散文措辞 / 表格样式 —— 只校验「声称集 == 真值集」这一件事。
- **非目标 2**：**不**把 README 变成权威来源；本票正是把 **workflow 文件**确立为权威，README 是**被校验方**。
- **非目标 3**：**不**动 `check-spec-refs.py` / `check-tasks-refs.py` / `check-journal-ledger.py` 的判据。
- **账 1（延续，T36 账 1）**：`paths` ↔ `Cargo.toml` 一致性**仍无守卫**（需 CI 有 Rust 工具链 + 先裁对账口径，仓内无法坐实）。
- **账 2（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；
  `.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- **账 3（本票新增）**：本守卫只覆盖「**触发路径**」这张表。README 里**其他**手抄的机器可读面
  （测试基线数、job 数等）**仍无守卫** —— 是否扩面：**待决**（先把这一张钉死，别一次铺太宽）。
