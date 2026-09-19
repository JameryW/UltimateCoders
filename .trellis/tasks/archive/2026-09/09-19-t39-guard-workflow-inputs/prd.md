# T39 —— 把「门禁依赖的输入文件必须全列进 `paths`」升格为守卫

> 承接 **T35 / #685** 立起、**T36 / #686** 只用过一次的铁律。issue **#689**。
> 前置一手复核（HEAD `7a78997`）：工作树干净；开放 issue **1**（#656，无新内容）；archive 最大 `-t38-`；issue 最大号 688。

---

## 一、缺口（一手读数，HEAD `7a78997`）

workflow 的 `run:` 步里**点名**的仓内文件就是那个 job 的输入。判据：**它必须在同一 workflow 的 `on.push.paths` 与 `on.pull_request.paths` 里被覆盖。**

实测 9 套（**结构化**抽取 `run:` 正文、**剥掉 shell 注释**）：

| workflow | `push.paths` | 抽取到的引用 | 未覆盖 |
|---|---|---|---|
| `ci-codex-flow.yml` | 9 | `scripts/check-codex-issue-flow.py` | 0 |
| `ci-dashboard.yml` | 2 | *(无)* | 0 |
| `ci-journal.yml` | 5 | `scripts/check-journal-ledger.py`、`tests/python/test_check_journal_ledger.py` | 0 |
| `ci-python.yml` | 11 | `crates/uc-python/Cargo.toml`、`tests/python/test_nats_live_dispatch.py` | 0 |
| `ci-readme-ci-table.yml` | 5 | `scripts/check-readme-ci-table.py`、`tests/python/test_check_readme_ci_table.py` | 0 |
| `ci-rust.yml` | 5 | `docker/docker-compose.yml` | 0 |
| `ci-scripts.yml` | *(无过滤)* | 5 条（guard ×3 + tests ×2） | 0（无过滤 ⇒ 恒真覆盖） |
| `ci-trellis.yml` | 4 | `.trellis/scripts/common/active_task.py`、`tests/python/test_task_finish_fallback.py`、`tests/python/test_archive_repoints_refs.py` | 0 |
| `ci-typescript.yml` | 4 | *(无)* | 0 |

⇒ **基线全绿**（**8** 套带过滤 workflow / **11** 条引用受覆盖判据 / **0** 未覆盖；
另 5 条引用落在无过滤的 `ci-scripts.yml` 上 —— 它每次推送都跑，故不可能漏）。
⚠️ 本行初稿写的是「7 套 / 12 条」，是**守卫自己的输出**纠正了它（`workflows: 9 workflow(s),
8 path-filtered, 16 run-step reference(s), 11 subject to coverage`）—— 手工点数的产物。

### 判据的形状（§ 与「全仓扫描」的区别）

- **结构化**：只取 `jobs.*.steps[*].run`（字符串），逐行**丢弃 shell 注释**（`^\s*#`）。
- ⚠️ **粗糙做法会误报**（实测）：对整份 YAML 扫「路径形状」token，会抓住**头注里的** `.trellis/.template-hashes.json`（3 处全是注释）与 `docs/agents/*.md`（散文里的**假 glob** —— 正是 T36 抓出的那条）。⇒ **注释不是输入。**
- **引用形状** = `第一段是仓内顶层目录 / … / 文件名.扩展名`。第一段用**声明的常量**（`TOP_LEVEL_DIRS`）而非「从文件树推导」—— 见 § 非目标 3。

---

## 二、历史背书（最强的取证形式：**真实**漂移，非合成）

把**同一段抽取 + 覆盖判定**代码跑在三个真实修订上：

| 修订 | 读数 |
|---|---|
| `130c343^`（T36 修之前） | ❌ `crates/uc-python/Cargo.toml` **未被 `paths` 覆盖** |
| `130c343`（T36 的修复） | ✅ 覆盖（`paths` 5 → 11 条） |
| `HEAD`（`7a78997`） | ✅ 覆盖 |

即：**本票的判据会抓到 T36 那次真实的 20% 盲区**（历史语料：218 条推送里 43 条让 Python 侧静默不跑）。
⚠️ 该对照**不进 pytest**：CI 的 `actions/checkout` 默认 `fetch-depth: 1` ⇒ 测试里取不到历史 blob。⇒ 它作为**可复算的工具脚本**留在记忆层（`.workbuddy/memory/tools/`），读数写进 journal 与 issue。

---

## 三、判据（各带独立失败消息 ⇒ 可逐条消融钉住）

| # | 判据 | 失败消息 |
|---|---|---|
| 1 | **非空性**：解析到 ≥1 套 workflow；≥1 套带过滤；**总共 ≥1 条引用** | `parsed 0 workflow(s)` / `no path-filtered workflow to check` / `extracted 0 reference(s) from 0 workflow(s)` |
| 2 | **`push` 覆盖**：每套带过滤 workflow 的每条引用都被 `on.push.paths` 覆盖 | `<wf>: push paths do not cover <ref>` |
| 3 | **`pull_request` 覆盖**：同上，对 `on.pull_request.paths` | `<wf>: pull_request paths do not cover <ref>` |
| 4 | **空 `paths` = 死门禁**：`paths` 存在但为空 ⇒ 该 workflow 永不触发 | `<wf>: paths filter is empty, so this workflow can never trigger` |

**两个手写组件各配正负对照**（T38 的教训：恒真的匹配器会让判据 2/3 永绿）：
- **路径匹配器**（`prefix/**` 前缀 + 精确相等 + 段内 `*`）：真理表 **4 正 / 3 负**。
- **引用抽取器**：真理表 **3 正 / 3 负**（负例必须含：**注释行里的路径**、URL 里的路径、非仓内顶层目录）。

---

## 四、交付形状

1. **新增 `scripts/check-workflow-inputs.py`**：4 条判据；根目录由 `__file__/../..` 推导 ⇒ **测试可整目录沙箱化，生产代码里没有 test-only 钩子**。
   - **输入集封闭** = `.github/workflows/*.yml`（**不读 README、不读全仓文件树**）。
2. **新增 `.github/workflows/ci-workflow-inputs.yml`**：单 job、矩阵 `["3.9","3.12"]`、`paths` = `.github/workflows/**` + 守卫 + 其测试。
   - ⚠️ `paths` 是 **workflow 级** ⇒ 独立小文件，**不动任何既有 workflow 的 `paths`/`steps`**。
   - 🔑 **本 workflow 自检**：它的 `run:` 点名 `scripts/check-workflow-inputs.py` 与 `tests/python/test_check_workflow_inputs.py`，两者都在它自己的 `paths` 里 ⇒ 它自己就是判据 2/3 的一个实例。
3. **新增 `tests/python/test_check_workflow_inputs.py`**：真仓 pin + **N 处单点突变**，并**断言 4 条判据每条至少被一处突变单独打到**。
4. **两版 README 补第 10 行 + 段首计数 `Nine`→`Ten` / 九→十**（推论 A：新增第 10 套 workflow 会让现表**必然少一行** ⇒ T37 守卫**必然先红**）。计数词表已含 `ten` / `十` ⇒ 无需改守卫。
5. `tests/python/test_check_tasks_refs.py` 钉值 `(793,794)` → **`(794,795)`**。

---

## 五、消融设计

| 方向 | 做法 | 期望 |
|---|---|---|
| **一 真实漂移（本票自带）** | 新增第 10 套 workflow ⇒ **T37 守卫必然少一行**；同时**本票守卫**把它当自己的一个实例检查 | T37 守卫两版各报一条 `workflows on disk but not in table`；补行后两者皆绿 |
| **二 历史对照**（不进 CI） | 同一段代码跑 `130c343^` / `130c343` / `HEAD` | 红 → 绿 → 绿 |
| **三 合成突变（沙箱）** | 一次一处，按字节恢复 | 每条判据**至少一处单独打到**，且无两条共享同一判词 |

合成突变草案：
- **A** `ci-python.yml` 的 `push.paths` 删 `crates/uc-python/**` ⇒ **判据 2** 红（判据 3 仍绿 —— 一次只动一个轴）
- **B** 同一条只从 `pull_request.paths` 删 ⇒ **判据 3** 红
- **C** 某 workflow 的 `paths` 改成 `[]` ⇒ **判据 4** 红
- **D** 沙箱内清空**所有** `run:` 块 ⇒ **判据 1** 红（`extracted 0 reference(s)`）
- **E** 往某 `run:` 里**新增**一条未覆盖的引用（真实漂移形状）⇒ **判据 2/3** 红
- **F** 给某 workflow 删掉整个 `paths` 块 ⇒ 它从此恒跑 ⇒ 应当**照绿**（**阴性对照**：判据不是「必须都有 paths」）

---

## 六、验收（逐条可复算）

| # | 验收 |
|---|---|
| 1 | 新守卫对当前真仓 `rc 0`。**开票时**（HEAD `7a78997`，9 套）：**9** workflow / **8** 套带过滤 / **16** 条引用（其中 **11** 条受覆盖判据）/ **0** 未覆盖；**终态**（本票加了第 10 套）：**10** / **9** / **18** / **13** / **0** —— 每对多出的那个都是本票自己的 workflow，守卫把它当作判据 2/3 的一个实例覆盖。测试钉的是**终态** |
| 2 | 4 条判据**各有独立消息**，且每条**至少被一处突变单独打到**（由覆盖断言**强制**） |
| 3 | **历史对照**：同一段代码在 `130c343^` 上红（报 `crates/uc-python/Cargo.toml`）、在 `130c343` 与 `HEAD` 上绿 |
| 4 | 两个手写组件**正负例都通过**（匹配器 4 正 / 3 负；抽取器 3 正 / 3 负） |
| 5 | 四个既有守卫 `rc 0`；`ruff check scripts/` 与 `ruff check python/ tests/` 通过 |
| 6 | 语料钉值 `(794,795)` 的**两个可达态都实测**；`test_check_tasks_refs.py` 通过；Python 总收集数 = **1215 + 本票新增用例数** |
| 7 | **不动任何既有 workflow 的 `paths`/`steps`** —— 既有 9 套的 `paths` 一条未改 |
| 8 | 两版 README 补第 10 行且计数改为 **Ten / 十**；T37 守卫绿 |
| 9 | CI：`ci-workflow-inputs.yml` **被触发**且两腿 success；T37 守卫的 workflow 也触发（`.github/workflows/**` 命中）；其余 workflow 判词与基线一致 |

---

## 七、非目标与账（未静默丢弃）

- **非目标 1**：**不**推导 `Cargo.toml` 的**语义**闭包（「改了 `uc-engine` 的语义就该跑 Python CI」）。需 CI 有 Rust 工具链 + 先裁对账口径 ⇒ **仍记账 1**。本票只做 `run:` 里**字面点名**的文件 —— 那是这条铁律**可落地的那一半**。
- **非目标 2**：**不**把 README 变成权威来源；本票是 **YAML ↔ YAML**。
- **非目标 3**：🔴 **不读全仓文件树**。若守读到「仓内有哪些文件」，它的判据就不再是封闭输入集 ⇒ 按 T35 判据，带 `paths` 过滤的 job 会**蒙住**它（删掉被引用文件时它不跑）。代价：`TOP_LEVEL_DIRS` 是**声明常量**，新增顶层目录必须**同 change** 加进去 —— 该代价**已记账 A**，且守卫**把常量打进输出**使遗漏可见。
- **非目标 4**：**不**校验 `docker compose` 的**传递挂载**（`docker/docker-compose.yml` 挂 `./tikv.toml`、`./nats.conf`，并挂 `../` 整个仓根）⇒ 传递闭包在此**不可行**（仓根 ⇒ 全仓）。本轮**实测并记录**，不实现。
- **账 A（本票新增）**：`TOP_LEVEL_DIRS` 是声明常量；新增顶层目录若忘记登记 ⇒ 该目录下的引用**不被抽取**（静默盲区）。缓解：常量打进输出 + 测试钉住常量内容。是否改成「从文件树推导」：与**非目标 3** 冲突，**记待决**。
- **账 B（本票新增）**：`run:` 里的引用**可能是产物**（如 `--out python/foo.py`）⇒ 会误红。本轮**不**引入豁免表（YAGNI，且空表的判据是装饰）；若真出现，再引入**带理由**的豁免表。
- **账 1（延续）**：**部分偿还**（字面引用这一半）；语义闭包那一半仍阻塞。
- **账 5（README `Checks` 列散文数字）**：本轮**实测**其为**语义映射**（`spec-refs` ↔ job 名 `spec reference guard`）且两版语言不同 ⇒ 字面对账会变成脆弱的散文解析 ⇒ **明确不作**，继续记 5。
- 账 2（`ruff format --check` 76 文件）、账 4（`add_session.py` index 行数估值）延续。
