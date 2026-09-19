# Journal - Jamery Wang (Part 3)

> Continuation from `journal-2.md` (archived at ~2000 lines)
> Started: 2026-09-19

---



## Session 45: T38 guard the README CI prose count and the footnote's two claims (#688)

**Date**: 2026-09-19
**Task**: T38 guard the README CI prose count and the footnote's two claims (#688)
**Branch**: `main`

### Summary

在既有 README<->workflow 对账守卫里加 3 条判据（段首计数词 / 脚注「每个带过滤 workflow 自身 YAML 命中自己的 paths」/ 脚注「all workflows support manual dispatch」）。三处缺口先实测：旧守卫四处全绿、新守卫四处全红（两方向消融）。守卫自己抓出新代码里的真 bug（英文数词表小写键 vs 句首 Nine）。

### Main Changes

## 缺口的一手读数（HEAD `303e019`）

T37 立起的判据是「**手抄的机器可读面必须有守卫**」，而它**只把这条判据用在了表格上**。同一个 § CI 段里还有三处手写陈述：

| 检查 | 命令 / 出处 | 读数 |
|---|---|---|
| 守卫覆盖了什么 | `check-readme-ci-table.py` 判据 1-7 | 只对账**表格**（9 行 × 触发路径） |
| 段首计数词 | 沙箱：`Nine independent workflows` → `Eight` | 守卫 **`rc 0` 照绿** ⇒ 缺口成立 |
| 脚注前半句 | 沙箱：删 `ci-dashboard.yml` 自身条目 | 守卫 **`rc 0` 照绿**，而该句**已为假** |
| 脚注后半句 | `yaml.safe_load` 逐文件查 `on` 键 | **9/9** 为真，**无人校验** |

## 最要紧的一处：T37 的对账口径是**减法**

守卫算 `truth = paths - {自身 YAML 文件名}` —— 也就是「README 那一格不必写自身，由脚注统一说明」。**减一个不在集合里的元素是空操作**：若某 workflow 不再列自己的文件，`truth` 不变、守卫照绿，而脚注那句「每个带过滤的 workflow 自身 YAML 也命中自己的 `paths`」**已为假**。

该前提在 T37 里是**写在注释里的假设**，从未被检查 ⇒ **T37 立的判据没被用到 T37 自己的脚注上**。本票把它从假设变成**被检查的条件**（判据 9）。

## 三条判据（都读同一封闭输入集，不新增依赖、不新增触发面）

| # | 判据 | 各自的失败消息 |
|---|---|---|
| 8 | 段首计数词 == workflow 文件数 | `prose says ...` / **`no workflow count found`**（**缺计数也是失败** —— 否则改一句措辞就能让判据静默退休） |
| 9 | 每个带过滤的 workflow，其 `paths` 覆盖**自身 YAML**（直接列名，或经 `prefix/**`） | `paths no longer covers its own workflow file` |
| 10 | 每个 workflow 可手动触发 | `no workflow_dispatch trigger` |

判据 9 需要一个**最小路径匹配器**（`**` 视作前缀、其余精确相等）。⚠️ 手写解析器必须配**正负对照**：恒真的匹配器会让判据 9 **永绿**，故测试里带一张真理表（3 正 / 2 负）。判据 8 的字面量解析同样受非空性约束：**缺失比错误更危险**，所以「找不到计数」直接失败。

## 两方向消融（唯一能证明缺口真实的形式）

同一批四处突变、同一个仓，**只有守卫版本不同**：旧守卫（用 `git show 303e019:...` 取出）**四处全绿**，新守卫**四处全红且判词各自正确**。

| 突变 | 旧守卫 rc | 新守卫 rc |
|---|---|---|
| EN 段首计数 `Nine` → `Eight` | **0** | **1** |
| ZH 段首计数改小 | **0** | **1** |
| 某 workflow 删掉自身条目 | **0** | **1** |
| 某 workflow 去掉 `workflow_dispatch` | **0** | **1** |

旧守卫**故意**从 `git show 303e019:...` 取，而**不是**在本次运行里 `cp` 一份 —— 理由见下面的收尾事故。

## 守卫自己抓出的真 bug（非空性设计生效）

首次跑真仓时**守卫变红**：`README.md: no workflow count found`。查下去是**新代码自己的 bug** —— 英文数词表是**小写键**，而 `Nine` 处在句首**大写** ⇒ 查表落空、返回 `None`。中文版先过了（中文数词表按设计就是那几个汉字）。这正是非空性设计要的行为：一句被改写的措辞**大声失败**，而不是让判据静默退休。

## 变更

1. `scripts/check-readme-ci-table.py`（**8023 → 12728 B**，LF，213 → 318 行）：加判据 8/9/10 + 最小路径匹配器；判据清单 7 → **10**。
2. `.github/workflows/ci-readme-ci-table.yml`（**112 → 116 行**，CRLF 工作树；仓内按 LF 存，`git ls-files --eol` 为 `i/lf w/crlf`）：**只改头注**（判据数 7 → 10 —— 代码超越了它）。**`paths` / `steps` 零变更**。
3. `tests/python/test_check_readme_ci_table.py`（**8704 → 12639 B**，LF，236 → 326 行）：加 1 个测试 + **5 处**新突变（I/J/K/L/M）⇒ 突变数 8 → **13**；**覆盖断言强制 11 条判词无一条落空**。
4. `tests/python/test_check_tasks_refs.py`（**20517 → 20907 B**）：语料钉值 `(792,793)` → **`(793,794)`**；docstring、注释同步；新增 T38 段。

## 收尾事故（诚实记录，T29 ④ 的变体）

做「注入一条无人到达的判据、看覆盖断言红不红」的消融时，本机把**带沙箱升级的命令执行了两次**，且**两次交错**：

1. 第二次执行的 `cp` 拍到的「备份」**已是第一次突变后的文件** ⇒ 同一次运行里取到的任何哈希**都无法自证恢复成功**（哈希对上了也可能对着突变态）。
2. 中途用 `write_text` 写文件，Windows 把它**整体翻成 CRLF**（327 个 CR）—— 又一处已记录过的坑。

⇒ 恢复改用**绝对量兜底**：去 CR 再删注入行，应恰为 **12639 字节 / 326 行**。两个数都是**事前预测、事后逐字命中**（不是「写完再看多少」）。事故本身有正面产物：覆盖断言**确实活着**（注入一条无人到达的判据后报 `judgments no mutation reaches: [...]`）。

## 账（未静默丢弃）

- **账 3 部分偿还**：计数词 + 脚注两句已接进守卫；§ CI 段 `Checks` 列的散文数字**仍无守卫** —— 它是**自由文本**，钉死它等于把一句话变成契约，**是另一个决定**，故**转记账 5**，不夹带进来。
- **账 5（本票新增）**：README 里其他手抄的机器可读面（`Checks` 列散文数字、job 数、测试基线数）仍无守卫 —— **待决**（先把 § CI 这一张钉死）。
- **账 4（延续）**：`add_session.py` 写进个人 index 的行数取自「骨架追加后、正文填充前」⇒ 本轮同样偏低，下次运行重算自愈；`journal-2.md` 已达 **2000 行**上限 ⇒ **本会话由脚本自动开 `journal-3.md`（Part 3）**，本轮实测。
- **账 2（本轮实测）**：`ruff format --check scripts/ python/ tests/` 报 **76 个文件**待改 —— 那是**大票**（一次性重排全仓），**不是小票**，按登记延后。
- **账 1（延续）**：`paths` ↔ `Cargo.toml` 一致性仍无守卫（需 CI 有 Rust 工具链 + 先裁对账口径，仓内无法坐实）。
- 延续：`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。

## 补记（收尾时 CI 抓到本地子集漏掉的一处）

推完账本提交后 **Journal CI 两腿变红**，而本地门禁全绿：`test_check_journal_ledger.py` 里有**两处把本台账的文件数钉死在 2**（`"this ledger: 2 file(s)"` 与 `len(ours) == 2`）。`journal-3.md` 是 **45 个 session 里第一次轮转**，这两处钉值从未需要移动过。

| 项 | 读数 |
|---|---|
| 台账本身错了吗 | **没有** —— 守卫自始就报 `3 file(s), 45 session(s), 0 placeholder line(s), 45/45 conforming` |
| 错的是什么 | 两处**描述它的字面量**：与 `test_check_tasks_refs.py` 的语料钉值**同种**（新产物必然移动的计数 ⇒ 必须同 change 更新） |
| 为什么本地没看见 | 本地跑的是**子集** —— 我只跑了本票直改的两个测试文件，**没跑台账自己的测试文件** ⇒ 两腿 CI 都看到的红，在本地不可见 |
| 补的动作 | 移动两处钉值（2 → 3）并写清来由；随后**本地跑满全套** `tests/python/`：**1205 passed, 10 skipped**（总数 1215，与 CI 的 `1207 + 8` 同量） |

⚠️ 事故 2（同一天第二次「执行两次」）：修钉值的补丁脚本**报错但已写盘** —— 带沙箱升级的命令被执行两次，其中一次走完了全程。`git diff` 是唯一可信的判词（显示两处编辑各恰好一次、446 = 441 + 5）。另外我在同一个行数算术上**连错两次**，最后改成**从编辑定义推导**而非手算。
### Git Commits

| Hash | Message |
|------|---------|
| `adb33ae` | `ci(workflows): guard the README CI prose count and the footnote's two claims (#688)` — 4 files, +219/−14（守卫 +113 / 测试 +92 / workflow 头注 +7−7 / 语料钉值 +14−4） |
| `d8cac38` | `chore(task): archive 09-19-t38-guard-readme-ci-prose` — 4 files, +206（含 `task.json`，归档提交按仓规带上） |
| `ef5792a` | `test(journal): move the two ledger file-count pins the Part 3 rotation invalidated (#688)` — 1 file, +7/−2 |

### Testing

- [OK] 新守卫对当前真仓 `rc 0`，报告 **`9 workflow(s) reconciled in 2 file(s)`**
- [OK] **两方向消融 4/4**：同一批四处突变、同一仓、只有守卫版本不同 —— 旧守卫（`git show 303e019:...` 取出）**四处全绿 `rc 0`**，新守卫**四处全红 `rc 1` 且判词各自正确**（EN 计数 / ZH 计数 / 删自身条目 / 去 dispatch）
- [OK] **13 处合成突变**（T37 的 A-H + 本票 I/J/K/L/M），沙箱内逐处施加、按字节恢复；**覆盖断言强制 11 条判词无一条落空**
- [OK] 覆盖断言**自身做了消融**：注入一条无人到达的判据 ⇒ 立刻报 `judgments no mutation reaches: [...]` ⇒ 「判据清单被覆盖」不是装饰
- [OK] 两个手写解析器各带**正负对照**真理表（路径匹配器 3 正 / 2 负；计数解析 2 正 / 1 负）—— 恒真的匹配器会让判据 9 **永绿**
- [OK] 四个既有守卫均 `rc 0`：`check-codex-issue-flow` / `check-spec-refs` / `check-tasks-refs` / `check-journal-ledger`
- [OK] `ruff check scripts/` 与 `ruff check python/ tests/` 均 `All checks passed!`；⚠️ 首轮实测出 **1 处真实 E501**（CJK 计数正则行 150 列），按隐式拼接拆行后输出逐字不变
- [OK] 语料钉值 `(793,794)` 的**两个可达态都实测到**：任务目录未跟踪 ⇒ **793 ok / 0 dangling / 0 malformed**；归档落盘后 ⇒ **794 ok / 0 dangling / 0 malformed**
- [OK] `tests/python/test_check_tasks_refs.py` **14 passed**；Python 总收集数 **1214 → 1215**（= 本票新增 1 例，逐字吻合）
- [OK] **CI（推送 `adb33ae`）**：9 套 workflow 中**恰好 3 套**触发 —— 新 workflow（**两腿 success**，各报 `9 workflow(s) reconciled in 2 file(s)` 且各 **`4 passed`**）、**Scripts CI**（**5/5 success**）、**Python CI**（**4/4 success**）
- [OK] CI 逐字读数：Python 两腿均 **`1207 passed, 8 skipped`**（基线 1206 + 本票 1 例）；tasks-refs job 内 **`793 ok / 0 dangling / 0 malformed`**（pin 的首个可达态）
- [OK] **验收 7 实测**：`git diff` 的 `.github/workflows/**` 部分**全是 `#` 注释行**，`paths` / `steps` 零变更（既有 9 套 workflow 的 `paths` 一条未动）
- [OK] **本地跑满全套** `tests/python/`：**1205 passed, 10 skipped**（总数 1215，与 CI 的 `1207 + 8` 同量；拆分差异来自本地无 PG/Docker 而多跳 2 个）
- [OK] **CI（推送 `ef5792a`，修完钉值）**：3 套触发 —— **Journal CI 两腿 success**（各报 `this ledger: 3 file(s), 45 session(s), 0 placeholder line(s), 45/45 session(s) conforming`）、**Python CI 4/4 success**（两腿逐字 `1207 passed, 8 skipped`）、Scripts CI success

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`adb33ae` 实现 + `d8cac38` 归档 + `8929db8` 账本 + `ef5792a` 钉值修复）；`ef5792a` 上三套 CI **全绿** ⇒ **#688 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：**守卫自己的对账口径也是前提，也要被检查**。T37 的 `truth = paths - {自身}` 把一句脚注**当成了公理**；本票把它变成判据 9。⇒ 写守卫时问一句：**我算这个集合时，减掉的/加上的东西，是我检查过的，还是我假设的？**
- **本票付的学费（写进技能）**：① **两方向消融**（旧守卫全绿 / 新守卫全红）比单向突变强 —— 它同时证明「缺口真实」与「修的就是这个缺口」；② **覆盖断言自身也要消融**，否则它可能本身就是自指断言；③ **绝对量兜底**在「备份已被污染」时是唯一可用的恢复依据（同一次运行里取的哈希无法自证）；④ 计数解析器**必须**把「找不到」当失败。
- **账 5（本票新增）**：§ CI 段 `Checks` 列的散文数字、job 数、测试基线数**仍无守卫** —— 待决。
- **账 4（延续）**：`add_session.py` 的个人 index 行数估值偏低（取自填充前），下次运行自愈；`journal-2.md` 满 2000 行 ⇒ 本会话起用 `journal-3.md`。
- **账 2（实测）**：`ruff format --check` 有 **76 个文件**待改 ⇒ **大票**，延后。
- **账 1（延续）**：`paths` ↔ `Cargo.toml` 一致性**仍无守卫**（需 CI 有 Rust 工具链 + 先裁对账口径，仓内无法坐实）。
- 延续：`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。


## Session 46: T39 guard that a workflow's run: steps are covered by its paths (#689)

**Date**: 2026-09-19
**Task**: T39 guard that a workflow's run: steps are covered by its paths (#689)
**Branch**: `main`

### Summary

把 T35/T36 立起、只被手工执行过一次的铁律升格为守卫：workflow 的 run: 步点名的仓内文件必须被自己 paths 覆盖。历史两方向消融（发布版守卫跑三修订：130c343^ 红 → 130c343 绿 → HEAD 绿）。测量改掉了实现（fnmatch 与 README 守卫的匹配器在两处不一致）。

### Main Changes

## 缺口的一手读数（HEAD `7a78997`）

workflow 的 `run:` 步里**点名**的仓内文件就是那个 job 的输入 —— 判据：**它必须在同一 workflow 的 `on.push.paths` 与 `on.pull_request.paths` 里被覆盖。**

T35 / #685 立起这条铁律，T36 / #686 **只手工执行过一次**（把 `ci-python.yml` 的 `paths` 从 5 条扩到 11 条）。本票把它变成机械判据。

**为什么它值一个守卫（T36 的读数，非推演）**：`ci-python.yml` 的 test job 用 maturin 编译 `crates/uc-python`，而它的 `paths` **一条 `crates/**` 都没有** —— 218 条推送里 **43 条（约 20%）**让 Python 侧引擎检查**静默不跑**。**门禁不跑时没有任何东西变红，这就是全部的失效模式。**

## 判据（4 条，7 条独立失败消息）

| # | 判据 | 失败消息 |
|---|---|---|
| 1 | **非空性**（三面）：解析到 ≥1 套 workflow；≥1 套带过滤；**总共 ≥1 条引用** | `parsed 0 workflow(s)` / `no path-filtered workflow to check` / `extracted 0 run-step reference(s)` |
| 2 | 每条引用被 `on.push.paths` 覆盖 | `<wf>: push paths do not cover <ref>` |
| 3 | 同上，对 `on.pull_request.paths` | `<wf>: pull_request paths do not cover <ref>` |
| 4 | **空 `paths` = 死门禁** | `<wf>: paths filter is empty, so this workflow can never trigger` |

**抽取必须是结构化的**（实测）：对整份 YAML 扫「路径形状」token 会抓住**头注里的** `.trellis/.template-hashes.json`（3 处全是注释）与 `docs/agents/*.md`（**只存在于散文里的假 glob**，正是 T36 抓出的那条）⇒ **注释不是输入**。所以：只取 `jobs.*.steps[*].run`，逐行丢弃 shell 注释行。

## 历史两方向消融（本票最强取证：**真实**漂移，非合成）

让**实际发布的守卫**跑在三个真实修订的语料上（不是重写一份抽取逻辑）：

| 修订 | rc | 逐字判词 |
|---|---|---|
| `130c343^`（T36 修之前） | **1** | `ci-python.yml: push paths do not cover crates/uc-python/Cargo.toml` + 同一条 `pull_request` 判词 |
| `130c343`（T36 的修复） | **0** | — |
| `HEAD` | **0** | — |

⇒ 判据**被历史背书**：它会抓到 T36 那次真实的 20% 盲区。⚠️ 该对照**不进 pytest**（`actions/checkout` 默认 `fetch-depth: 1` ⇒ 测试时取不到历史 blob），作为**可复算工具**留在 `.workbuddy/memory/tools/t39-historical-ablation.py`。

## 测量改变了实现（两处，都是真 bug）

初版 `covered_by` 用 `fnmatch` + `prefix/**` 快捷式；与 `check-readme-ci-table.py` 的匹配器逐例对照，**恰好两处不一致**：

| pattern | path | README 守卫 | 新守卫（初版） | 谁对 |
|---|---|---|---|---|
| `scripts/*.py` | `scripts/sub/a.py` | `False` | `True` | **README**（`fnmatch` 的 `*` 会跨 `/`） |
| `scripts/**` | `scriptsX/a.py` | `False` | `True` | **README**（前缀匹配无边界 —— **#678 同款坑**） |

两者都会让本守卫与 README 守卫对**同一份 YAML** 给出不同判词 ⇒ 改为段内作用域 glob，并由 `test_the_two_path_matchers_agree` 把两份副本钉死。⚠️ 该测试**必须**同时断言「表内既有 `True` 也有 `False`」—— 否则**两个恒 False 的匹配器也算一致**。

## 真实漂移（本票自带，非合成）

新增第 10 套 workflow 让两版 README 的表**必然少一行** ⇒ `ci-readme-ci-table.yml` **当场先红**：两版各报 `prose says Nine workflow(s), but 10 exist` + `workflows on disk but not in table: ['ci-workflow-inputs.yml']`；补第 10 行与计数词后转绿。**两态都亲眼看到。**

## 新产物移动了别处的钉值（铁律又一次生效）

第 10 套 workflow 把计数词从 `Nine` 移到 `Ten`，而 `test_check_readme_ci_table.py` 里两处突变锚点**引用 README 的真实措辞** ⇒ 锚点失效。**锚点计数断言在锚点处大声失败**（`anchor occurs 0x, expected 1`），而不是静默替换成**空操作**（那会让该测试**保绿却什么都没钉**）。已移动并加注释记录该耦合。

## 突变按**分支**计数，不按个数

4 处语料突变（push 丢文件 / pull_request 丢文件 / push 变空过滤 / pull_request 变空过滤）+ 3 个**退化沙箱**（空 workflows 目录 / 无带过滤 workflow / 抽不到引用）覆盖 7 条判词 —— **断言的是「判词清单被覆盖」而非突变数**。两个**阴性对照**必须保绿：无过滤的 workflow、既不监听 push 也不监听 pull_request 的 workflow（否则守卫在报一条没人同意的政策）。两条**元断言**（判词覆盖 / 一致性表非空）各自在**一次性副本**上做了消融 —— **不改被跟踪文件**（上一票学到：被打断的消融会留下突变态，连备份都可能是突变态）。

## 变更

| 文件 | 读数 |
|---|---|
| `scripts/check-workflow-inputs.py`（新增） | **10204 B** / 272 行，LF-only；根目录由 `__file__/../..` 推导 ⇒ 测试可整目录沙箱化，生产代码无 test-only 钩子 |
| `.github/workflows/ci-workflow-inputs.yml`（新增） | **5076 B** / 105 行，CRLF；单 job、3.9+3.12、`paths` 三条（`.github/workflows/**` + 守卫 + 其测试）⇒ **自身就是判据 2/3 的一个实例**（它点名的两个文件都在自己的 `paths` 里） |
| `tests/python/test_check_workflow_inputs.py`（新增） | **16707 B** / 402 行，LF-only；**6 例** |
| `tests/python/test_check_readme_ci_table.py` | **12934 B**（+295）—— 两处计数锚点 `Nine`→`Ten`（第 10 套 workflow 所致）+ 注释记录耦合 |
| `tests/python/test_check_tasks_refs.py` | **21324 B**（+414）—— 语料钉值 `(793,794)` → **`(794,795)`** + T39 段 |
| `README.md` / `README.zh-CN.md` | **28243 B** / **25257 B**（+243 / +248）—— 补第 10 行 + 计数 `Ten` / 「十套」 |

### Git Commits

| Hash | Message |
|------|---------|
| Hash | Message |
|------|---------|
| `841a97d` | `ci(workflows): guard that the files a workflow's run: steps name are covered by its paths` — 7 files, **+800/−9**（守卫 +272 / 测试 +402 / workflow +105 / 两版 README 各 +2−1 / 语料钉值 +11−3 / T38 测试 +7−3） |
| `d3a6103` | `chore(task): archive 09-19-t39-guard-workflow-inputs` — 4 files, **+167**（`task.json` 33 / `prd.md` 126 / `implement.jsonl` 6 / `check.jsonl` 2） |

⚠️ `d3a6103` 是**意料之外地**出现的：`task.py archive --no-commit` 之后我跑了「`git add` + `git commit`」，本机把该命令**执行了两次** —— 第一次完整提交（`d3a6103`），第二次报 `nothing to commit`。⇒ **`git log` / 文件字节才是一手事实**，不要从 `commit` 的输出推断「没提交」。

### Testing

- [OK] **历史两方向消融**（最强，非合成）：**发布版**守卫跑三修订语料 —— `130c343^` **rc 1** 且精确点名 `crates/uc-python/Cargo.toml`（push + pull_request 各一条）；`130c343` **rc 0**；`HEAD` **rc 0**
- [OK] **真实漂移**：第 10 套 workflow 让两版 README 各报 `prose says ... but 10 exist` + `workflows on disk but not in table` ⇒ 补行后 `readme-ci-table check passed.`（两态都实测）
- [OK] **6 例测试全过**：真仓 pin（钉 `workflows: 10 workflow(s), 9 path-filtered, 18 run-step reference(s), 13 subject to coverage`）/ 判词覆盖 / 阴性对照 / 两个解析器的正负对照 / 一致性表 / 恢复独立性
- [OK] **两条元断言各自消融**：注入一条无人到达的判据 ⇒ 报 `judgments no mutation reaches: ['bogus-control']`；把一致性表改成只剩正例 ⇒ 报 `the table is vacuous: only {True}`
- [OK] 两个手写组件**正负对照**：匹配器 4 正 / 3 负（含实测出的两处分歧）；抽取器 3 正 / 3 负（URL / 非顶层目录 / 无文件形状），并钉住 `run_texts` **丢弃 shell 注释**且忽略非 `run:` 步
- [OK] 六守卫均 `rc 0`：`check-codex-issue-flow` / `check-readme-ci-table` / `check-workflow-inputs` / `check-spec-refs` / `check-tasks-refs` / `check-journal-ledger`
- [OK] `ruff check` 三条全绿（本票两文件 / `scripts/` / `python/ tests/`）
- [OK] 语料钉值 `(794,795)` 的**两个可达态都实测**：任务目录未跟踪 ⇒ **794 ok**；归档提交落盘后 ⇒ **795 ok**（`0 dangling / 0 malformed`）
- [OK] `test_check_tasks_refs.py` **14 passed**；Python 总收集数 **1215 → 1221**（= 本票新增 6 例，逐字吻合）
- [OK] **本地跑满全套** `tests/python/`：**1211 passed, 10 skipped = 1221**（不跑子集 —— 上一票正是漏在子集上）
- [OK] **CI（推送 `841a97d`）**：10 套中**恰好 4 套**触发 —— 新 workflow（**两腿 success**，各报 `workflows: 10 workflow(s), 9 path-filtered, 18 run-step reference(s), 13 subject to coverage` 且各 **`6 passed`**，3.9 腿跑通 ⇒ `from __future__ import annotations` 前提**仍成立**）、**README CI Table CI**（success）、**Scripts CI**（**5/5**）、**Python CI**（**4/4**）
- [OK] CI 逐字读数：Python 两腿均 **`1213 passed, 8 skipped`**（基线 1207 + 本票 6）；tasks-refs job 内 **`scanned 794 / 794 ok`**（pin 的首个可达态）+ `SELF-CHECK PASSED: 6 mutations, all distinct`
- [OK] **CI（推送 `d3a6103`，归档）**：**恰好 1 套**触发（Scripts CI）—— **5/5 success**，tasks-refs 报 **`scanned 795 / 795 ok`**（pin 的第二个可达态）
- [OK] **验收 7 实测**：`git diff` 的 `.github/workflows/**` 部分**为空** ⇒ 既有 9 套 workflow 的 `paths`/`steps` 一条未改；唯一改动是**新增一个文件**
- [OK] 告警核对：新 workflow 的 pytest 报 `PytestConfigWarning: Unknown config option: asyncio_mode` —— 与 **T38 的姊妹 job 同一告警**（CI 只装 `pyyaml pytest`）⇒ **既有、非本票引入**，不夹带修

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`841a97d` 实现 + `d3a6103` 归档），两笔提交的 CI **全绿** ⇒ **#689 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：**一条只被手工执行过一次的铁律，就是一条没有守卫的铁律。** T35 立了规则、T36 手工用了它一次 —— 而**手工用过一次**与**有守卫**之间的差距，正是本票的量。
- **本票付的学费（写进技能）**：① **两方向消融可以用历史语料做**（`git show <rev>:<path>` 取真语料，喂给**发布版**守卫）—— 比合成突变强，因为它证明的是**真实发生过**的漂移；② **重复实现同一契约的两份代码必须有「一致 + 非空」的双重断言**（只断言一致，两个恒 False 的实现也算一致）；③ **锚点钉在「会被别处改动的东西」上时，锚点计数断言是唯一能防止空操作突变的东西**；④ **「命令执行两次」是本机常态** ⇒ `commit` 的输出不是判词，`git log` 与文件字节才是。
- **账 A（本票新增）**：`TOP_LEVEL_DIRS` 是**声明常量**，新增顶层目录若忘记登记 ⇒ 该目录下的引用**不被抽取**（静默盲区）。缓解 = 常量打进输出 + 测试钉住内容。是否改为「从文件树推导」与**非目标 3**（不读全仓文件树，否则守卫自身不能被 `paths` 过滤）冲突 ⇒ **记待决**。
- **账 B（本票新增）**：`run:` 里的引用**可能是产物**（如 `--out python/foo.py`）⇒ 会误红。本轮**不**引入豁免表（空表的判据是装饰）；真出现再引入**带理由**的豁免表。
- **账 1 部分偿还**：只覆盖「字面点名」这一半；`Cargo.toml` 的**语义闭包**（改了 `uc-engine` 就该跑 Python CI）仍阻塞（需 CI 有 Rust 工具链 + 先裁对账口径）。
- **非目标 4（本轮实测并记录）**：`docker/docker-compose.yml` 挂 `./tikv.toml`、`./nats.conf` 并挂 `../`（仓根）⇒ 传递闭包在此**不可行**，不实现。
- 账 2（`ruff format --check` **76 文件** ⇒ 大票）、账 4（`add_session.py` 个人 index 行数取自填充前，下次自愈）、账 5（README `Checks` 列散文数字 —— 语义映射 + 两版语言不同 ⇒ **明确不作**，继续记）延续。
- 延续：`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- #656（P2 本体）仍只等外部「方案第 21 章」原文。


## Session 47: T40 guard every tracked file against mixed line endings (#690)

**Date**: 2026-09-19
**Task**: T40 guard every tracked file against mixed line endings (#690)
**Branch**: `main`

### Summary

把「逐票手工实测行尾」升格为全仓守卫：5 个混合行尾文件（合计 7149 处 lone LF）整改为 LF，新增 scripts/check-line-endings.py（3 条判据）+ 测试，job 落在无 paths 的 ci-scripts.yml。修前 rc 1 恰好 5 条具名失败、修后 rc 0；CI 两腿逐字与本地一致。

### Main Changes

## 缺口的一手读数（HEAD `9624968`）

5 个**已跟踪**文件的工作树副本自相矛盾（同一文件里既有 CRLF 又有裸 LF），合计 **7149 处 lone LF**：

| 文件 | CRLF | lone LF |
|---|---|---|
| `dashboard/index.html` | 12 | 1 |
| `dashboard/src/grpc/engine_pb.ts` | 102 | 3532 |
| `packages/uc-orchestrator/src/grpc/engine_pb.ts` | 102 | 3532 |
| `tests/python/test_dashboard_metrics.py` | 566 | 65 |
| `tests/python/test_worker_capabilities.py` | 230 | 19 |

**为什么没人看见（实测，非推演）**：`core.autocrlf=true`（来自沙箱 PortableGit 的**系统** gitconfig，不是本仓的选择）在 `git add` 时把 CRLF 归一成 LF，于是混合的工作树与它自己的 index blob **仍然相等**：

- `git diff` / `git diff --cached` 对「只差行尾」的残留是**空**的；
- `git status --porcelain` 修之前报**干净**；把 5 个文件归一之后反而报 ` M`，而 `git diff` 依旧空、`git diff-files --raw` 的目的 sha **全是 0**、`git update-index --refresh` 说 "needs update"，一次 `git add` **什么都没暂存**就把状态清掉了。

⇒ **`git status` 对行尾不携带任何方向的信息**，「工作树是干净的」**不是**内容判据。

**缺口在作用域与武装，不在知识**：`check-journal-ledger.py` 早就在算每个 journal 的 `crlf` / `lone_lf`，但它只覆盖 journal，且自己的 docstring 写着 "printed, never a verdict"。

## 三条判据（5 条独立失败消息）

| # | 判据 | 失败消息 |
|---|---|---|
| J1 | **非空性**（两面）：扫到 ≥1 个跟踪文件；二进制分类器**确实**跳过了一些 | `scanned N tracked file(s) (expected >= 1)` / `no binary file was skipped -- the classifier is broken` |
| J2 | **index blob** 不混行尾（跨平台确定） | `<path>: index blob mixes line endings (n CRLF, m lone LF)` |
| J3 | **工作树副本**不混行尾 | `<path>: working tree mixes line endings (n CRLF, m lone LF)` |

**只判「混不混」，绝不判「该用哪种」**：后者是逐文件属性且**环境相关**（本机 checkout CRLF、CI checkout LF）⇒ 写成契约会在 CI 上错。J3 在 CI 上绿是因为 CI 的 checkout **确实**是齐的（正确的判词，不是空判）；而一旦有人从非 autocrlf 机器提交了混合 blob，它以混合形态落地 ⇒ J3 立刻红。

## 真实漂移（本票自带；合成突变只是补充）

修之前跑**发布的**守卫：**rc 1，恰好 5 条具名失败**，计数与上表手算**逐字一致**。修之后 **rc 0**。

5 个文件统一为 **LF** —— 而这正是 index 一直都在存的（实测 **5/5** 与各自 index blob 逐字节相同）⇒ **它们不出现在本票任何 diff 里**，唯一证据是守卫从 5 条具名失败变成 0 条。

## 测量改变了实现（两处）

- **gitlink 不是 blob**：`git cat-file --batch` 用 `:<path>` 读 `vendor/oh-my-pi` 会失败（mode `160000`，内容是一个 commit）⇒ 初版守卫报出**第 6 条假失败**。改为 `git ls-files --stage -z` 暴露 mode 字节、跳过 `160000`，并单独报 `gitlink(s) skipped: N`。
- **突变机制换了一种**：原计划靠「删掉 NUL 探测」打红 J1，实现改成**删掉那个二进制 fixture** —— 同一条消息变红，但不需要改被测代码，也不会与「第二条突变打红同一条消息」混淆。

## 门禁接线（为何是 job 而不是第 11 套 workflow）

全仓游走的守卫（输入集 = 整个已跟踪 index）在 T35 判据下**不能带 `paths`**（带过滤只会对翻转判词的改动失明）⇒ 必须落在无过滤的 `ci-scripts.yml`。做成 **job** 而不是新 workflow：workflow 计数保持 **10** ⇒ T37 的 README 表格与计数词**一字不动**，本票因此**不碰 README**。既有 9 套 workflow 的 `paths`/`steps` **一条未改**（`git diff` 的 `.github/workflows/**` 只有这一个文件，且只有新增）。

## 实现期两处新发现（都是跑出来的）

1. **新产物移动了另一个测试文件的钉值**（T39 那条教训重演）：新 job 的 `run:` 步新增 2 个**去重后**的文件引用 ⇒ `test_check_workflow_inputs.py` 的真仓摘要 `18 run-step reference(s)` → **20**。该值由 10 套 workflow 文件的**内容**决定，与本票新增多少跟踪文件无关 ⇒ 是**单值**（不是可达对），同 change 改掉。判据：跑测试时它**当场变红**。
2. **任务目录的提交时机决定 `implement.jsonl` 该写哪个路径**：实测 T39 —— 四个任务文件**全**加在归档提交（`d3a6103`），实现提交里**一个都没有**（逐个 `git log --diff-filter=A` 查证）。而语料 = **已跟踪**集合，且要求被引用的 `.trellis` 目标**也**已跟踪 ⇒ 实测「只暂存 jsonl、不暂存 `prd.md`」会得到**真的** `795 ok / 1 dangling`。于是：实现提交里任务目录**整体保持未跟踪**（语料停在 795），且 `implement.jsonl` **从一开始就写归档后的路径**（`archive/2026-09/...`），否则 `task.py archive` 一移动目录，引用立刻悬挂。
3. 顺带实测到一个**会被误读的中间态**：任务目录只暂存一半时守卫报 `tracked file(s): 1839, text scanned: 1830` —— **这个值 CI 永远不会看到**（任务文件是整批在归档提交里落地的）⇒ 计数钉值仍然只有两个可达态 `(1836,1827)` 与 `(1840,1831)`。


### Git Commits

| Hash | Message |
|------|---------|
| `1d27a1d` | (see git log) |
| `6e36424` | (see git log) |

### Testing

- [OK] **修前 rc 1、恰好 5 条具名失败**，计数与手工复算**逐字一致**（12/1、102/3532、102/3532、566/65、230/19）；**修后 rc 0**
- [OK] **5 个文件逐字节复核**：`CR == 0`，且与各自 index blob **逐字节相同 5/5** ⇒ 它们**不出现在本票任何 diff 里**
- [OK] 新增测试 **5 passed**：真仓 pin（两条内容属性钉死 + 文件计数钉成两个可达态）/ 5 文件保持 LF-only 且与 index 逐字节同 / 四条突变覆盖断言 / 二进制分类器**正反两向** / 与 `git ls-files --eol` 的跨实现「**一致 + 非空**」
- [OK] 每条判据被**独立**突变打红，且打红集合**不相交**（A 造工作树混合 ⇒ **不得**报 J2；B 塞 index 混合 blob ⇒ **不得**报 J3）
- [OK] 六守卫均 `rc 0`：`check-spec-refs` / `check-tasks-refs` / `check-journal-ledger` / `check-readme-ci-table` / `check-workflow-inputs` / `check-line-endings`
- [OK] `ruff check` 三条全绿（本票两文件 / `scripts/` / `python/ tests/`）
- [OK] **语料钉值 `(795,796)` 的两个可达态都实测**：任务目录未跟踪 ⇒ **795 ok**；归档提交落盘后 ⇒ **796 ok / 0 dangling / 0 malformed**
- [OK] **行尾守卫计数钉值的两个可达态都实测**：**1836/1827**（实现提交，任务目录未跟踪）与 **1840/1831**（归档后）
- [OK] `test_check_tasks_refs.py` **14 passed**；三个受影响测试文件合计 **25 passed**
- [OK] **本地跑满全套**（不跑子集）：1226 collected = **1215 passed + 10 skipped + 1 failed**；那 1 条是沙箱 `safe-delete` 的**每轮**批量删除计数（`count 1819 > threshold 50`）在 temp 清理时触发 ⇒ **环境产物、非回归**（该文件**单独跑 27/27 通过**）
- [OK] **Python 总收集数 1221 → 1226**（= 本票新增 5 例，逐字吻合）
- [OK] **CI（实现提交 `1d27a1d`）恰好 4 套触发、全绿**：**Scripts CI 7/7**（含新 job **两腿** success）、**Python CI**（dashboard checks + ruff lint + 两腿 test 全 success）、**README CI Table CI**（success）、**Workflow Inputs CI**（success）；**Rust / TypeScript 正确地未触发** —— 5 个归一文件与 index 逐字节相同 ⇒ 没有任何 TS 路径变动
- [OK] **CI 逐字判词与本地一致**：两腿都报 `tracked file(s): 1836, text scanned: 1827, binary skipped: 9, gitlink(s) skipped: 1` + `line endings check passed.`，各 **5 passed**、ruff `All checks passed!`；3.9 腿跑通 ⇒ `from __future__ import annotations` 前提**仍成立**
- [OK] CI 上那条 `PytestConfigWarning: Unknown config option: asyncio_mode` 与**姊妹 job 同一告警**（该 job 只装 `ruff pytest`）⇒ **既有、非本票引入**，不夹带修
- [OK] 验收 6 实测：`git diff` 的 `.github/workflows/**` 只有 `ci-scripts.yml` **一个文件**、且**只有新增**（`paths` 一条未改）

### Status

[OK] **Completed**

### Next Steps

- [OK] **已完成** —— 两笔提交（`1d27a1d` 实现 + `6e36424` 归档）的 CI **全绿** ⇒ **#690 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：**`git status` 不是内容判据。** 判「工作树是否干净」只认 `git diff`；判「行尾有没有残留」只能读字节。
- **本票付的学费（写进技能）**：① **`core.autocrlf` 把「行尾」变成 git 结构性看不见的一层** ⇒ 任何字节级判据都不能拿 `git status` / `git diff` 做代理；② **`git ls-files --stage` 的 mode 字节**是区分 gitlink 与 blob 的唯一可靠来源（`:<path>` 读不了 submodule）；③ **只判客户能判的那一半** —— 环境相关的属性（该用哪种行尾）不进契约，只判不变量（混不混）；④ **一个新 `run:` 步引用新文件，会移动另一个测试文件的钉值**，与 T39「README 计数词移动了另一个测试文件的突变锚点」**同形** ⇒ **加产物先 grep 谁钉了它**。
- **账 6（本票新增，未偿）**：**没有 `.gitattributes`**。本票只修残留、**不立策略**：加属性文件会把「checkout 用哪种行尾」升为全仓策略并影响下次 clone 的 1600+ 文件，需要独立证据与决策；且本沙箱的 `autocrlf=true` 是**环境注入**的，加属性文件会让环境差异从「显式」变「静默」。
- **账 7（本票新增，未偿）**：守卫的 **J3 是本机与 CI 判词可能合法分叉的唯一一条**（本机 worktree CRLF、CI worktree LF）。今天两侧都绿且**双方都是正确判词**，但它意味着**「本机绿」不能推「CI 绿」**（反之亦然）。已把理由写进守卫 docstring；真出现分叉时按「只判 mixed、绝不判 which」复核。
- **账 8（本票顺手实测出的账，未偿）**：T39 的 journal（Session 46）把 `.trellis/scripts/**` 写成 **27**，而 `ci-scripts.yml:39` 与今天的实测都写 **28**（`git ls-files .trellis/scripts | grep -c '\.py$'` = 28 = 6 顶层 + 22 嵌套）。差 1 —— 正是「手抄的机器可读面必须脚本对账」（T36 那条）的又一例。⚠️ **归档 journal 是历史记录，不回改 T39 的 Session 46**，在此登记更正即可。
- ⚠️ 顺带实测的**锚点陷阱**（值得记住）：`git ls-files '.trellis/scripts/**/*.py'` 返回 **22**，而 `git ls-files .trellis/scripts | grep '\.py$'` 返回 **28** —— pathspec 里的 `**/` 在本机 git 上**不匹配顶层文件**，于是同一个问题有两个「正确答案」。⇒ **数「全仓某类文件」时不要用 `**/` 前缀**。
- 账 1（`docker compose` 挂载闭包）、账 2（`ruff format --check` **78** 文件 ⇒ 大票）、账 4（`add_session.py` 个人 index 行数取自填充前，下次自愈）、账 5（README `Checks` 列散文 —— 语义映射 + 两版语言不同 ⇒ **明确不作**）延续。
- 延续：`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`（**28** 个跟踪 `.py`，其中恰好 1 个被 lint，按登记排除）与 `.claude/hooks/**`（3）按登记排除。
- #656（P2 本体）仍只等外部「方案第 21 章」原文。
