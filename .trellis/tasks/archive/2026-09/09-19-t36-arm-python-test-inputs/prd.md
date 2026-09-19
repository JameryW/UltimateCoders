# T36 PRD —— 把 uc-python 依赖闭包加进 `ci-python` 的 `paths`（该 job 编译它们却不被它们触发）

- **票**：T36 ／ issue **#686**
- **承接**：T35 / #685 立起的规则 **「门禁依赖的输入文件必须全列进 `paths`」**，本条是把它用在一个**既有** job 上
- **依赖**：**不依赖**外部「方案第 21 节」原文（框架卫生线）
- **起点一手复核**（不继承上一轮叙述）：`HEAD = origin/main = de35f0b`、工作树干净；
  开放 issue = **1**（#656，3 条评论、末次更新 `2026-09-18T13:45Z`、无新内容）；归档最大 `-t35-` ⇒ 本票 **T36**；
  issue 最大号 685 ⇒ 本票 **#686**。

---

## 结论

| # | 结论 |
|---|---|
| A | `ci-python.yml` 的 `test` job 用 `maturin develop --release --manifest-path crates/uc-python/Cargo.toml` **编译 Rust**，再 `pytest tests/python/ -v`；而其 `paths`（`python/**`、`tests/**`、`pyproject.toml`、`dashboard/**`、自身）**一条 `crates/**` 都没有** ⇒ **只改 `crates/**` 的推送让 Python 侧对引擎的检查静默不跑** |
| B | 闭包是**推导**出来的：`crates/uc-python/Cargo.toml` 的 `[dependencies]` = `uc-types` + `uc-engine`（`storage`+`indexing`）+ `uc-grpc` ⇒ 实际编译 **4 个 crate + 工作区根 `Cargo.toml`/`Cargo.lock`** |
| C | **`uc-grpc-server` 不在闭包内**（独立 binary crate）⇒ 修正**必须是**这 6 条精确条目，**不是** `crates/**`（后者是过度触发） |
| D | 历史语料消融：**模拟器逐行复刻 GitHub 判词**（现行 `paths` 命中 **65/218** = API 里 Python CI 真跑的 **65/218**）；A 违例 **0**；B = **43/218 的推送改了闭包而旧 `paths` 完全瞎、其中 Python CI 真跑 0 条、新 `paths` 命中 43/43**；C 结构性 5/5 |
| E | 单提交直证：`1e61318`（`crates/uc-engine/**`，push head）API `total_count = 1` ⇒ **只有 Rust CI** |
| F | 这**不是**已决冻结：T26 的「三选项」是关于 **`scripts/**`** 的裁决（`scripts/**` 不是该 job 的输入 ⇒ 选 C 正确）；触发面表（09-16）把两个触发集写成**互不相交**，**没有**把它记成欠账 |
| G | 本票会**移动被钉死的语料计数**：新票 `implement.jsonl` 进语料 ⇒ `check-tasks-refs` +1，钉值 `(790,791)` → **`(791,792)`**，同 change 更新 |
| H | 暴露面有限但真实：只有 **2 个** 测试文件引用 extension（`test_async_engine.py` 25 条、`test_affinity_placement.py`），且 `engine.py:17` 的 import 在 `try/except ImportError` 里 ⇒ 严重度「中」，不是「致命」——**本票按「输入集必须等于触发面」修，不夸大成语义缺陷** |
| I | **推论 A 同票修**：`README.md:397` / `README.zh-CN.md:362` 把 Python CI 的触发面写成 `` `python/`、`tests/`、`pyproject.toml` `` —— 改完 `paths` 后该陈述**变为假**。**顺带实测发现该段整体早已过期**：`README.md:392` 写「**Two** independent CI workflows」，而仓内实有 **8** 套 workflow、且触发是「**推送到 `main` 与面向 `main` 的 PR**」（原文只说 PR）⇒ 整段重写而不是只改两格 |

---

## 一、缺口的一手读数（HEAD `de35f0b`）

| 检查 | 命令 / 出处 | 读数 |
|---|---|---|
| 该 job 是否编译 Rust | `ci-python.yml:68-73` | `maturin develop --release --manifest-path crates/uc-python/Cargo.toml` |
| `paths` 是否含 crates | `ci-python.yml:7-20`（`push` + `pull_request`） | **0 条** `crates/**` |
| 该 `paths` 是否**曾**含 crates | `git log -p -- .github/workflows/ci-python.yml \| grep '^[+-].*crates'` | 唯一命中是**那一步**（`maturin … crates/uc-python/Cargo.toml`），**没有一条 `paths` 条目** ⇒ **从未列过** |
| 闭包推导 | `crates/uc-python/Cargo.toml` `[dependencies]` | `uc-types`、`uc-engine`（`default-features=false, features=["storage","indexing"]`）、`uc-grpc` |
| 反向确认 | 同文件：**无** `uc-grpc-server` | `uc-grpc-server` 是独立 binary crate ⇒ 不在闭包内 |
| 其他根级构建配置 | `git ls-files 'rust-toolchain*' '.cargo/**' 'clippy.toml' 'rustfmt.toml' '**/build.rs'` | 只有 `crates/uc-grpc/build.rs`（**在** `crates/uc-grpc/**` 内）⇒ 闭包无遗漏项 |
| Python 侧是否真消费 | `python/ultimate_coders/engine.py:17` | `from ultimate_coders._uc_core import PyEngine, PySearchQuery`（`try/except ImportError` 兜底 `None`） |
| 谁在测它 | `git grep -ln 'ultimate_coders.engine\|PyEngine\|_uc_core' -- tests` | **2** 个文件：`test_async_engine.py`（25 条，本地 **25 passed in 0.61s**，**不在** 8 个 skipped 里）、`test_affinity_placement.py` |
| 暴露面大小 | `git grep -c '#\[pyclass\]\|#\[pymethods\]' -- crates/uc-python` | `engine.rs` / `scheduler.rs` / `types.rs` 三文件 + `lib.rs` 的 `#[pymodule]`，合计 **30+ 处**装饰器 |

---

## 二、历史语料消融（可复算；**语料与索引同源**）

**语料构造**（从 PRD 可复现）：

1. `gh api "repos/…/actions/runs?branch=main&per_page=100&page=N"`（N=1,2,3）⇒ **300 runs / 219 push heads**。
2. push 的改动集 = `git diff --name-only <上一个 push head>..<本 push head>`；**非祖先对**单独报告（本次 **0**）。
3. **地面真值** = API 里该 head 是否存在 `Python CI` run —— 与改动集计算**无关**（真触发面命中就必然有 run）。
4. 匹配器把 `paths` 的 glob 逐条对上改动集（`a/**` 视作前缀 `a/`；非 glob 条目精确相等）。

| 方向 | 读数 | 判读 |
|---|---|---|
| **模拟器自校（关键对照）** | 现行 `paths` 命中 **65/218**；API 显示 Python CI 跑了 **65/218** | **逐行一致** ⇒ 匹配器复刻了 GitHub 的判词，故它对**新** `paths` 的预测可信 |
| **A 不丢覆盖** | Python CI 跑过的 65 条，新 `paths` **全部仍命中**（违例 **0**） | 只加不删，没有把任何已在跑的推送挤掉 |
| **B 本票要修的洞** | 改了闭包而旧 `paths` **命中 0** 的推送 = **43** 条；其中 Python CI **真跑了 0 条**；新 `paths` **命中 43/43** | 「改动集 ⊄ 触发面」= **43/218 ≈ 20%** 的推送 |
| **C 不过度触发** | 语料内「只动闭包外 crate」的推送 = **0** ⇒ 该方向**在语料上为空**（如实记录），改用**结构性断言**（见下） | 不把「语料没覆盖」谎报成「已验证」 |

C 的结构性断言（不依赖语料）：

| 合成改动集 | 新 `paths` 命中 | 期望 |
|---|---|---|
| `crates/uc-engine/src/x.rs` | ✅ | 命中（闭包成员） |
| `crates/uc-python/src/lib.rs` | ✅ | 命中（闭包成员） |
| `Cargo.lock` | ✅ | 命中（工作区根） |
| `crates/uc-grpc-server/src/x.rs` | ❌ | **不命中**（闭包外） |
| `crates/uc-grpc-server/Cargo.toml` | ❌ | **不命中**（闭包外） |

⇒ 5/5 符合预期；这同时证明了「为什么不是 `crates/**`」。

**单提交直证**：`gh api ".../actions/runs?head_sha=1e6131845ee5faaf3bc0c65b1aac3937430d2ffe"` ⇒ `total_count = 1`，
唯一 run = **Rust CI**。

---

## 三、为什么是「加闭包」而不是别的

| 方案 | 判读 |
|---|---|
| **A 加 6 条闭包条目（本票）** | **正确的最小形状**：触发面 = 该 job 真正的输入集；不新增 job/workflow，不复制 setup |
| B 新开一个「seam」workflow 只跑 2 个 extension 测试文件 | **不采纳**：`paths` 是 workflow 级 ⇒ 新 workflow 需要自列 `crates/**` + `python/**` + `tests/**` + 自身（否则改测试文件又不触发，造出**新的**盲区）；而省下的只是 `pytest` 那 ~31s —— **成本大头在 `maturin` 构建与 venv/setup，两者都省不掉** ⇒ 换来的是第三份 setup 与一个新的触发面要维护 |
| C 只加 `crates/uc-python/**` | **不采纳**：`uc-engine` 的**语义**变化会改变 extension 的行为而不改 `uc-python` 一行 —— 这正是 T33「相邻两层各测各的 ⇒ 接缝无人守」的形状 |
| D 加 `crates/**` | **不采纳**：`uc-grpc-server` 不在闭包内 ⇒ 是**过度触发**（语料里有 2 笔只动它的提交） |
| E 不动，只记账 | **不采纳**：这是**既有 job 的输入集不全**，不是「可选的额外覆盖」；T26 选 C（独立 workflow）之所以正确，是因为 `scripts/**` **不是**该 job 的输入 |

---

## 四、验收（逐条可复算）

| # | 验收 |
|---|---|
| 1 | `ci-python.yml` 通过 `yaml.safe_load`；`paths` 条目 **11**；`push` 与 `pull_request` **逐条相同** |
| 2 | **job 数与 step 数与改前逐字相同**（只动 `paths`，不动 job/step） |
| 3 | 消融读的是**工作树里的 workflow 文件**（脚本解析 YAML，不持有副本）⇒ 索引与语料同源 |
| 4 | 模拟器自校 **65 = 65**；A 违例 **0**；B **43/43**（其中 Python CI 跑过 **0**）；C 结构性 5/5 |
| 5 | 既有四个守卫（`codex-flow` / `spec-refs` / `tasks-refs` / `journal-ledger`）均 `rc 0`；`ruff check scripts/` 仍 `All checks passed!` |
| 6 | `test_check_tasks_refs.py` 通过；钉值 `(791,792)`；Python 总收集数 **不变** |
| 7 | **不改任何其他 workflow**（`git diff --stat` 只含本票文件） |
| 8 | CI：推送后 `ci-python.yml` 被触发且 success；Rust / Scripts / Journal / Trellis 判词与基线一致 |
| 9 | README 两版的 CI 段：**8 行**工作流表格；每行**声称的路径集 == 该 YAML 的 `paths` 去掉自身文件名**（两份 README 各跑一次对账脚本，`rc 0`） |
| 10 | 该对账脚本**先红过后绿**：写表时它当场抓出我写的 `` `docs/agents/*.md` ``（YAML 里其实是 4 个具名文件，**没有这个 glob**）⇒ 修正后转绿。再对**沙箱副本**做 5 处单点突变（**A** 行内加假路径 / **B** 行内删真路径 / **C** 表里改名不存在的 workflow / **D** 只改 YAML 不改 README / **E** 只改 `push` 不改 `pull_request`），**5/5 变红**、**无两条共享同一判词** ⇒ 该脚本的每条分支都被钉住，不是装饰 |

---

## 五、非目标与账（未静默丢弃）

- **非目标 1**：**不**重构 `ci-python.yml` 的 job/step 结构，**不**动其他 workflow 的 `paths`/触发面。
  （**文档**在两版 README 内按推论 A 更新；**工作流文件本身**一行未动。）
- **非目标 2**：**不**扩到 `crates/uc-grpc-server/**`，也**不**改 Rust CI 的触发面。
- **非目标 3**：**不**声称 `paths` 的完整性可由此一劳永逸 —— 闭包是**从 `Cargo.toml` 推导**的；将来 `uc-python`
  新增依赖，`paths` 必须**同 change** 更新。文件头写明这条维护规则。
- **账 1（本票新增）**：`paths` 与 `Cargo.toml` 的一致性**没有守卫**（第二个 crate 加进来时无人提醒）。
  是否值得写一个「从 `cargo metadata` 推导闭包并与 `paths` 对账」的守卫：**记账待决** —— 它需要 CI 里有
  Rust 工具链且对账口径（是否含 dev-dependencies / build-dependencies）需先裁，**仓内无法坐实** ⇒ 不臆造。
- **账 2（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；
  `.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- **账 3（本票新增）**：**README 的 CI 段与 YAML 之间没有守卫**。本票为了满足推论 A 把该段重写成
  **8 行 × 全路径**，等于新造了一处**会漂移的重复**（它刚漂移了 6 套 workflow 没人发现）。对账脚本
  （`readme_ci_<...>.py`）已完成并**通过 5 轴突变自检**，但**留在 `.workbuddy/tmp/`（被 ignore）、未入库**。
  是否把它升格为正式守卫（`scripts/` + `ci-scripts.yml` 的 job + `tests/python/` 的用例）：**记账待决**。
  口径已探明：它能读**封闭输入集**（写死的两份 README + `.github/workflows/*.yml`）⇒ `paths` 过滤对该 job
  **是正确的**；但 `paths` 是 workflow 级、`ci-scripts.yml` 无过滤且其 job 必须保持无过滤 ⇒ 该 job 只能
  放进 `ci-scripts.yml` 并同样无过滤（守卫本身廉价，可接受）。**留待裁**。
