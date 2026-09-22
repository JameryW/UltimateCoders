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


## Session 48: T41 widen the spec-reference guard to docs/ and fix the anchors it exposed (#691)

**Date**: 2026-09-20
**Task**: T41 widen the spec-reference guard to docs/ and fix the anchors it exposed (#691)
**Branch**: `main`

### Summary

把 check-spec-refs.py 从单根（.trellis/spec）扩为多根（+docs）：探针实测 docs 下 34 refs / 40 mentions、2 条 fail-closed PATH_FORM；扩面后引用 98→134、提及 227→267；修掉 4 处真漂移锚点 + 2 处 PATH_FORM；新增命名空间隔离断言与 8 个测试（消融 3 突变打红 9/1/4，各有私有见证）；CI 两腿与本地逐字同。

### Main Changes

## 缺口的一手读数（HEAD `ed9f19c`，2026-09-20）

守卫的覆盖根是**硬编码单根**：`SPEC_DIR = ROOT / ".trellis" / "spec"`（`:92`），`collect()` 只 `rglob` 那一根（`:464`）。
而 `docs/**` 里同样是**手写 `path:line` 引用**，同样无人守。

**决定性探针**：只把 `SPEC_DIR` / `_SPEC_PREFIX` 指向 `docs/`、守卫一字未改：

| 项 | 读数 |
|---|---|
| 纳入文档 | **8** 个 `.md` |
| `path:line` 引用 | **34** = OK 25 / STALE 6 / **PATH_FORM 2** / AMBIGUOUS 1 |
| 无行号路径提及 | **40** = resolved 36 / **DANGLING 2** / MENTION_AMBIGUOUS 2 |

**扩面即红**：`main():684` 的 fail-closed 集合是 `{MISSING_FILE, PATH_FORM, OUT_OF_RANGE}`，2 条 `PATH_FORM` 足以让它 `return 1`。
两条都正是守卫自己 docstring 举的那个例子形状 —— 带目录却只按后缀解析：

| 文档 | 引用 | 实际解析为 |
|---|---|---|
| `assessment.md:121` | `sandbox/agents/claude_code.rs:221` | `crates/uc-engine/src/sandbox/agents/claude_code.rs` |
| `assessment.md:142` | `uc-types/src/agent.rs:210-232` | `crates/uc-types/src/agent.rs` |

## 为什么现在没人发现（两条，都实测过）

1. **作用域窄，不是知识缺口。** 守卫对「结构判据看不见漂移」的分析比 docs 的实际情形还准 —— 它自己的 docstring 写着
   *"a census on 2026-09-16 found 149 references of which **0** were out of range, so purely structural checks
   cannot see the drift at all"*。docs 侧同样是 **0 越界**，但**多出 2 条 PATH_FORM** ⇒ 守卫**能**看见，只是一直没看。
2. **CI 接线成本是零。** `check-spec-refs.py` 跑在 `ci-scripts.yml:107`（`--audit`），而该 workflow **刻意没有 `paths`**
   （头注：*NO `paths` filter … the guard walks the whole repository*）⇒ 扩面**不需要改任何 `paths`**。
   对照 T35 判据：`paths` 对**封闭输入集**正确、对**全仓游走**是蒙眼布；本守卫扩面后输入集仍是开放集，且它所在的 workflow 本就无过滤。

## 判据：只改作用域，一条判词都不动

| 类别 | 判词 | 本票变化 |
|---|---|---|
| 结构，fail-closed | `MISSING_FILE` / `PATH_FORM` / `OUT_OF_RANGE` | 判据不变，**输入集扩到 `docs/**`** |
| 咨询，永不失败 | `AMBIGUOUS` / `STALE` / `CONTENT_MISMATCH` | 判据不变 |
| 提及，咨询 | `DANGLING` / `MENTION_AMBIGUOUS` | 判据不变；docs 的 2 条进豁免表 |

**扩面实测**：引用 **98 → 134**（承载文件 **11 → 13**）；提及 **227 → 267**（承载文件 **26 → 32**）；结构违规 **0 → 2 → 0**。

## 多根特有的一条新约束：命名空间隔离

两张豁免表（`MENTION_EXEMPT` / `SUBJECT_REMOVED`）按「**相对 `.trellis/spec/`**」的 short 键控 —— 这在单根下天然无歧义
（docstring `:595-597` 明写是为了让 `backend/directory-structure.md` 与 `frontend/directory-structure.md` 不撞）。
多根后**同一个 short 可以在两个根下各存在一个文件**，于是为一边写的规则会**静默盖住**另一边。
⇒ 处置不是「写注释说不会撞」，而是**在 `exemption_self_check` 里对语料断言**：同一 short 不得出现在两个根下。
今天实测 `.trellis/spec/` 只有 `backend/frontend/guides`、`docs/` 只有 `agents/architecture/workflows` ⇒ **零碰撞**，但断言已就位。

`_short_of()` 同时把「导入时冻结」的 `_SPEC_PREFIX` 改成**调用时求值**，这顺手修好了合成语料夹具（它 monkeypatch 的正是这两个常量）。

## 修掉的锚点（推论 A：过期文档同票修）

| 原引 | 改为 | 依据（一手读） |
|---|---|---|
| `graph_store.rs:806-808` | **`:920-930`** | :806 是结构体字面量尾部 + T10 文档注释；`CREATE TABLE execution_events` 在 :920，三列在 :928-930 |
| `graph_store.rs:2290` | **`:2615`** | :2290 在 `merge_grants` 邻域；事件 INSERT 在 :2615 且**已**绑 `cost/tokens/duration_ms` |
| `types.py:67-80` | **`:297`** | :67 已是 T15 的 `SubtaskUsage`；`SubtaskResult` 在 :297，其 `usage` 字段在 :316 |
| `graph_store.rs:1030/1038` | **`:1167/1175`** | :1030/1038 分别是 `mirror_rejected` 与 `connect` 的注释；两个 INSERT 变体（shadow / 非 shadow）在 :1167 / :1175 |
| `sandbox/agents/claude_code.rs:221` | 补 `crates/uc-engine/src/` | PATH_FORM |
| `uc-types/src/agent.rs:210-232` | 补 `crates/` | PATH_FORM |

§90 的结论（「今天缺的是契约与写入点」）已被 **T15 / #660** 补齐 ⇒ **加日期标记而非改写**（那是**当日**的缺口记录）。

## 一处**本票自己造成**的「行号合法、内容错误」

recon 文档引了 `scripts/check-spec-refs.py:233`（豁免串所在行）。本票往该文件插入约 65 行后，那串搬到 **250** ——
**行号仍在范围内**，所以两个结构判据都不管（`OUT_OF_RANGE` 只管越界），`--audit` 的 STALE 也只认「命中的符号定义在别处」。
**是人工读一手时发现的。** ⇒ 同票改成 `:250`。这条直接印证了本票的必要性：**「改文件」与「改引用」之间目前没有守卫。**

## 门禁与接线

| 项 | 读数 |
|---|---|
| 守卫 | `scanned 134 path:line references in 13 spec files`；`summary: 118 ok / 7 stale(advisory) / 9 ambiguous(advisory) / 0 structural failure(s)`；mentions `43 of 267 resolve to no file (43 exempt, **0 unclassified**)` |
| CI 三方对账 | 本地 / CI **3.9** / CI **3.12** 三处**逐字相同**（上列三行完全一致） |
| ruff | 本票 4 个 Python 文件 `All checks passed!` |
| 其余守卫 | 七道全 rc 0 |
| workflow | `ci-scripts.yml` 仍**无 `paths`**；`jobs` 数与既有 steps 未动，仅头注 +15 行；`check-workflow-inputs` 报 `10 workflow(s), 9 path-filtered, 20 run-step reference(s)`（与 T40 后态一致） |

## 消融（三处**不同分支**的突变，各带私有见证）

| 突变 | 打红 | 私有见证 |
|---|---|---|
| M1 只扫第一根 | **9** | 7 个（two-roots / docs 的 A·B·C / 必要条件证明 D / docs 豁免 F） |
| M2 关掉命名空间断言 | **1** | `test_a_short_name_cannot_serve_two_namespaces` |
| M3 `_locate_all` 恒空 | **4** | `test_subject_removed_banner_controls_the_exemption`、`test_real_corpus_has_no_untriaged_dangling_mention` |

三组**不是全相交**，且每个分支都有**只属于它的红灯** ⇒ 无装饰分支。
恢复后由**独立进程**复算 sha256 = `4bfb86887053c5b89e9d60069cea729619b03d73d1b5a3a8447a0249fd1bc7c9`，与突变前一致。
新增测试 **8** 个（12 → 20）；其中 D 是**必要条件证明** —— 去掉第二个根后同一语料**一条都不报**。

### Git Commits

| Hash | Message |
|------|---------|
| `f1453e0` | `ci(scripts): widen the spec-reference guard to docs/ and fix the anchors it exposes (#691)`（实现，7 文件 +240/-26） |
| `f88c4d4` | `chore(task): archive 09-20-t41-guard-docs-refs`（归档，自动提交） |
| `9fde653` | `docs(task): record T41's archived commit, restore the trailing newline, and move the corpus pin to (796, 797)` |
| `77947ae` | `docs(journal): record session 48 (T41 / #691)` |
| `527c3d6` | `test(scripts): move the line-endings pin to the reachable pair (1844, 1835)`（修 CI 红） |

### Testing

- [OK] **全量 pytest**：`1234 collected` = **1223 passed / 1 failed / 10 skipped**。唯一失败
  `test_check_journal_ledger.py::test_untracked_journal_is_invisible_to_the_index` **单独复跑 27/27 passed**
  ⇒ 沙箱 safe-delete 的**累积**计数所致（与 T40 同因），**非回归**。
- [OK] **本票测试文件**：`test_check_spec_refs.py` **20 passed**（+8 新测试）；`test_check_tasks_refs.py` **14 passed**（钉值 (796, 797) 已对齐）。
- [OK] **七道守卫**全 rc 0；`check-line-endings` 报 `tracked file(s): 1840, text scanned: 1831, binary skipped: 9, gitlink(s) skipped: 1`。
- [OK] **ruff**：本票 4 个 Python 文件 `All checks passed!`。
- [OK] **消融**：M1/M2/M3 分别打红 **9 / 1 / 4**，各有私有见证；恢复后由**独立进程**复算
  sha256 = `4bfb86887053c5b89e9d60069cea729619b03d73d1b5a3a8447a0249fd1bc7c9`（与突变前一致）。
- [OK] **CI 4/4 绿（只对实现提交 `f1453e0` 成立，不覆盖本 session 其余提交 —— 见下条）**：Scripts CI（含 spec-refs 双 Python 腿）/ Python CI（`1226 passed, 8 skipped` ×2）/ Workflow Inputs CI / README CI Table CI。
  Rust 与 TypeScript **未触发**（本票无相关改动）。CI 的 **3.9** 与 **3.12** 两腿与本地**逐字相同**：
  `scanned 134 path:line references in 13 spec files` / `scanned 267 line-free path mentions in 32 spec files (201 resolved / 43 dangling / 23 ambiguous)` /
  `summary: 118 ok / 7 stale(advisory) / 9 ambiguous(advisory) / 0 structural failure(s)`。
- 🔴 **`9fde653` 与 HEAD `77947ae` 的 Scripts CI 实际都是红的**（我推 `9fde653` 时只验过 `f1453e0`）：
  两腿逐字 `AssertionError: the scan size has moved: ('1844', '1835')` —— **T41 自己的归档任务目录多出 4 个
  跟踪文件，把 `test_check_line_endings.py` 的计数钉值搬走了**。修法是 `527c3d6`：钉值
  `{(1836,1827),(1840,1831)}` → `{(1840,1831),(1844,1835)}`（T40 的 `(1836,1827)` 判为**不可达而删除**，
  与 `test_check_tasks_refs.py` 同一口径）。两个可达态的**两种独立计数互证**：守卫自身输出，以及
  `git ls-files` 1845 − 1 个 gitlink = 1844 = **1835 文本 + 9 二进制**；非空性钉值（`binary skipped: 9` /
  `gitlink(s) skipped: 1`）未动。该断言的**两个方向都在本轮观测到**：集合外的值报红、集合内的值转绿。
- 🔴 **新铁律（本 session 亲手踩到）**：「**CI 绿**」只对**验过的那棵树**成立 —— 实现票的 CI 绿**不覆盖**
  归档提交与 journal 提交。**每个提交各自要等 CI**，否则红会拖到票关闭之后才被发现。

### Status

[OK] **Completed**

### Next Steps

- **T41 / #691 交付完成**：实现 → 归档 → 账本/journal → `527c3d6` 修正 CI 钉值，**CI 全绿后才关票**；
  #656（P2 本体）仍只等外部「方案第 21 节」原文。
- 🔴 **新账（本票亲手踩到）**：**「改一个被文档按行号引用的文件后，必须 grep 谁引了它」目前没有守卫。**
  本票往 `scripts/check-spec-refs.py` 插入了约 65 行，把 recon 文档引用的 `:233` 推移到 `:250` ——
  **行号仍在范围内 ⇒ 两个结构判据都不报，STALE 也只认「符号定义在别处」**，靠人工读一手才发现。
  这是「改文件」与「改引用」之间的接缝，值得单独一张票。
- 🔴 **账 9（同一类，跨守卫）**：**归档一张票会移动「别的」守卫测试里的计数钉值**。T39→T40 动的是
  `test_check_tasks_refs.py` 的语料对，本票又动了 `test_check_line_endings.py` 的扫描计数对 ——
  两者都是「+4 个跟踪文件」引起的。⇒ 归档提交前先 `grep -rn "tracked file(s)\|len(refs)" tests/ scripts/`
  并按 **整行相等** 判据自查，或直接跑满全套（行尾守卫在 CI 上独立成腿，本地单跑守卫脚本**看不见**这条）。
- 延续：账 1（`docker compose` 挂载闭包）、账 2（`ruff format --check`，**从可复算读数重新起算**）、
  账 4（`add_session.py` 个人 index 行数取自填充前，下次自愈）、账 5（README `Checks` 散文，**明确不作**）、
  账 6（无 `.gitattributes`）、账 7（J3 本地/CI 合法分歧）、账 8（`27` vs `28` 计数更正）。
- ⚠️ **`task.py archive` 的两个已知缺陷再次实测成立**：`commit` 不写（本次手工补 `f1453e0`）、
  `task.json` **丢末尾换行**（已恢复）；`id` 由创建后手工补成全目录名，归档未破坏。
- ⚠️ **`task.py validate` 对 post-archive 的 `prd.md` 路径报 not found**：T40 既定取舍（先写归档后路径），
  CI 不跑 `validate`（已 grep 确认），属已知可接受中间态。
- ⚠️ **沙箱升级会让命令执行两次**（T38 记录）本票复现一次：写盘脚本**报错但已写盘**，
  逐项核对确认 4 处编辑**各落盘恰好一次**、无重复插入。


## Session 49: T42 expose references whose line number cannot be verified (#692)

**Date**: 2026-09-20
**Task**: T42 expose references whose line number cannot be verified (#692)
**Branch**: `main`

### Summary

守卫有两条咨询锚，但只有一条对行号敏感：`STALE` 比的是符号**定义行**是否落在被引范围内，
而内容锚只问「被引代码在不在文件里出现过」（**文件级**）。全量实测 134 条引用里 **60 条（44%）**
两条锚都不可能有 ⇒ 它们的行号**无法被任何判据 falsify**。四组实验坐实：A 逐条改行号 **0/60 可见**；
B（改文件名 ⇒ rc 0→1）/C（把定义移出范围 ⇒ stale 7→6）双对照证明探针本身有效；D 是 T41 刚修过的那条真实漂移，同样不可见。
新增 **`UNANCHORED`** 咨询类（只计数，永不进 verdict）；另修一处假阳性：`PATH_SPAN_RE` 少 `md` ⇒
`.md:134` 形状的**指针**被当成「被引代码」⇒ 那句 `1 of 134 have no matching quoted content` 全是噪音。
语料侧补 4 条符号锚 + 降级 4 条 `:1` 指针：refs **134→130**、mentions **267→271**、UNANCHORED **60→53**、假 mismatch **1→0**。
10 个新测试；8 处突变打红集合**两两不同**且 10 条新测试每条至少被打红一次；实现提交与归档提交**各自** CI 两腿绿。

### Main Changes

## 缺口：两条锚，只有一条看得见行号

| 锚 | 判据 | 对行号敏感？ |
|---|---|---|
| `STALE` | 行上符号的**定义行**是否落在被引范围内 | **是** |
| `CONTENT_MISMATCH` | 被引代码是否在目标文件里**出现过** | **否**（文件级） |
| 提及类（`DANGLING` 等） | 文件是否存在 | **否**（与行号无关） |

⇒ 一条引用若两者都没有，其行号**无法被任何判据反证** —— 这不是「知识缺口」，是**判据缺位**。

## 四组实验（全在 HEAD `59509e1`，同一提交）

| 实验 | 做法 | 读数 | 结论 |
|---|---|---|---|
| **A** | 60 条逐条把行号改成同范围内**另一个**值 | **0/60 输出变化**，rc 恒 0 | 沉默是结构性的，不是探针哑了 |
| **B** 机制对照 | `worker.py:524` → `worker_typo.py:524` | rc **0 → 1** | 探针会响 |
| **C** 判词对照 | `error-handling.md:320` 的 `sandbox.py:1007` → `:1324` | 输出变化，stale **7 → 6** | 行号确实被读 |
| **D** 真实漂移 | `p2-recon.md:67` 的 `check-spec-refs.py:250` → `:290` | rc 0，stdout **逐字节相同** | 真实漂移也看不见 |

## 为什么不开「重写那 60 条」这扇门（实测依据）

只有 **4 条**能补出「定义**就落在**被引范围内」的符号锚 —— 只有这种锚**真能验行号**；
其余 **56 条**即便硬补也只能得到**文件级**内容锚（**仍然验不了行号**）。有测量依据才动，本票只让洞**可见**。

## 第二处缺陷（量 D 时撞出来的）

`PATH_SPAN_RE` 没有 `md`，而 `PurePath('x.md:134').suffix == '.md:134'`（不是 `.md`）⇒
**两个解析器都拒绝**这个形状 ⇒ 一个 `path:line` **指针**落进内容候选分支，被当成「被引代码」，
而它**永远不可能**出现在目标里。这就是那句 `1 of 134 have no matching quoted content` 的全部来源，
且正好落在 `p2-recon.md:67` —— T41 刚修过漂移的那一行。

## 判据（本票新增）

`unanchored = not content_candidates and best is None`，默认只打印计数，`--audit` 列清单，**永不进 verdict**。

⚠️ 承重的是 `best is None`（**没有任何被点名的符号有可比较的定义**），而**不是**「行上没有符号」：
抽取器会把路径 token 咬成伪符号（`worker.py:524` → `worker`），而它没有定义可对。
口径写成后者，普查会掉近一半 —— 这就是突变 M2，也是「普查」与「口径」必须**并列断言**的原因。

## 语料（推论 A：代码长过了文档，就同票把文档搬过去）

| 文件:行 | 动作 | 结果 |
|---|---|---|
| `database-guidelines.md:64` | 补符号锚 | `ShortTermMemory` @45，`OK` |
| `database-guidelines.md:131` | 补符号锚 | `list_keys` @270，`OK` |
| `hook-guidelines.md:19` | 补符号锚 | `AgentEventType` @35，`OK` |
| `hook-guidelines.md:109` | 补符号锚 | `refresh_heartbeat` @158，`OK` |
| `type-safety.md:26` | 4 条 `:1` 指针**降级**为提及 | 首行是 import，`:1` 不携带信息；提及仍受 `MENTION_RESOLVED` 检查 |

## 读数

| 量 | 实现前（`59509e1`） | 实现后 | 说明 |
|---|---|---|---|
| `refs` | 134 | **130** | −4：降级的 `:1` 指针 |
| `mentions` | 267 | **271** | +4：同一批 |
| 无匹配引文（假 mismatch） | 1 | **0** | 内容候选过滤器修好 |
| `UNANCHORED` | 60（口径度量，未输出） | **53** | 新可见 |
| `ok / stale / ambiguous / structural` | 118 / 7 / 9 / 0 | **114 / 7 / 9 / 0** | 实现前由 134−7−9 推得 |

**53 的分解**：61 − 4（补好符号锚的）− 4（降级的 `:1` 指针，它们**本身就是无锚的**）= 53。
计划里估的是 57 —— **偏大 4**，因为漏算了降级那 4 条同时是分子。

## 消融：8 处突变，打红集合两两不同

| 突变 | 打到哪条分支 | 打红 |
|---|---|---|
| M1 `unanchored = False` | 新咨询类本身 | A / D / 普查 |
| M2 口径写成「行上有符号」 | 判据口径 | D / 普查（**A 必须仍绿**） |
| M3 `PATH_SPAN_RE` 丢 `md` | 内容候选过滤器 | E1 / E2 / 普查 |
| M4 让行号对无锚行重新可见 | 必要条件证明的敏感性 | **B** / A / 两条既有测试 |
| M5 丢掉「定义是否落在范围内」 | 阳性对照 | **C**（独此一条） |
| M6 把语料修复改回无名符号 | 修复是活的 | repaired / 普查 |
| M7 把 `:1` 指针改回来 | 降级是活的 | line_one_pointers / 普查 |
| M8 让被引代码在目标里消失 | 真仓内容钉 | content（独此一条） |

**两处与计划不符（据实记录）：**

1. **计划 M1/M2 的第一版打红同一集合** ⇒ 被脚本自己的「同一集合即装饰」断言拦下。
   原因：合成测试 A 当时用裸基名 `worker.py:2`，提取器会咬出符号 `worker` ⇒ A 与 D 同形，两条口径分不开。
   修法：A 改成**带目录**的路径（行上无符号），于是 **M1 红 A、M2 不红 A**。
2. **计划 M3「假 mismatch 回到 1」不成立**：光复原过滤器**已经不能**复现症状 ——
   本票给守卫写的头注里就含 `md:134` 这个例子，而内容测试只问「这个子串在不在目标里」——
   **记录缺陷的散文把缺陷掩住了**。故症状侧另配 M8，机制侧由 E1 钉；这条限度已写进那条真仓测试的 docstring。

## 归档为何只有一个提交（流程改进）

两处钉值都在**归档那一刻**移动（语料 = tracked 集合），所以**单归档提交必然 CI 红** —— T41 为此连红两轮。
本票把钉值同步**并入归档提交**：`line-endings` `(1844,1835) → (1848,1839)`（4 个文本文件），
`tasks-refs` `797 → 803`（**首个 delta > 1 的票**：`implement.jsonl` 贡献 4 条 `.trellis` 路径、`check.jsonl` 2 条，
因为本票的 jsonl 不只引用自己的 prd，还引用它作证据的 3 个 spec 文件 —— 分解已按源文件核实并写进 docstring）。

### Git Commits

| Hash | Message |
|------|---------|
| `2527764` | fix(scripts): expose references whose line number cannot be verified, and stop reading `x.md:134` pointers as quoted code (#692) |
| `8af77e0` | chore(task): archive 09-20-t42-unanchored-refs |

### Testing

- [OK] `python scripts/check-spec-refs.py` **rc 0**：130 refs / 271 mentions / 114 ok / 7 stale / 9 ambiguous / 0 structural / **0** content mismatch / **53 UNANCHORED**
- [OK] `pytest tests/python/test_check_spec_refs.py -o addopts=""` → **30 passed**（原 20）
- [OK] 全量 `PYTHONPATH=python pytest tests/python -o addopts=""` → **1234 passed, 10 skipped**（收集 1244 = 原 1234 + 本票 10，只看总数对账）
- [OK] 消融 8 处突变：打红集合两两不同；10 条新测试每条至少被打红一次；恢复由**独立进程**按字节 + sha256 复核（8 个突变残留串各 0 次）
- [OK] Scripts CI / Python CI 对 `2527764` 与 `8af77e0` **各自** success
- [OK] 归档同提交内同步钉值后：line-endings `1848/1839` 通过、tasks-refs `803 ok / 0 dangling / 0 malformed`、ruff 干净、三个守卫测试文件 **49 passed**

### Status

[OK] **Completed**

### Next Steps

- 账：`CONTENT_MISMATCH` 只到**文件级** —— 「引用的代码还在文件里，但行号已经指到别处」**仍无判据**。
- 账：符号抽取器仍会把路径 token 咬成伪符号；本票只在 `UNANCHORED` 判据里绕开它，**没有**修抽取器。
- 账：`check-tasks-refs-selftest.py` 本机必中断（只信 CI）。
- ⚠️ 复现提示：CRLF 文件里做**多行锚点**必须用 `"\r\n".join([...])` —— 本会话为此栽了三次（锚点恒 0 次匹配）。


## Session 50: T43 the unchecked-line-number caliber is 104/121, not 53/130 (#693)

**Date**: 2026-09-20
**Task**: T43 the unchecked-line-number caliber is 104/121, not 53/130 (#693)
**Branch**: `main`

### Summary
T42 把「没有任何锚」的引用暴露出来了，但它给出的**性质**比它数出来的**集合**大：summary 行写的是
"so a changed line number cannot be detected"，`--audit` 头部写的是 "cannot be verified at all"。
内容锚是**文件级**的 —— `_content_anchor(spec_line, body)` 的实现就是 `if form in body`，**签名里根本没有行号**
—— 所以「有内容锚、但没有符号锚」的引用**同样**行号不可检，而它被排除在 53 之外。

本票把这条性质单独命名（`LINE-UNCHECKED`），`UNANCHORED` 降为它的**下位层级**，两个数一起打印：
**104 / 121 located** 的行号不可检（其中 **53** 完全无锚），只有 **17 / 121** 的行号真的被比较过。
口径变的是措辞与可见性，**不是**判据强度：仍然只打印、永不进 verdict。

### Main Changes

- `scripts/check-spec-refs.py`：新增行字段 `line_unchecked = best is None`；docstring 新增 `LINE-UNCHECKED`
  条目（四段证据 a/b/c/d），并把 `UNANCHORED` 里那句 "its line number is unverifiable by construction"
  收窄为下位情形、附 `NOTE (T43/#693)` 说明「收窄的是声称，不是数字」。
- 默认 summary 行与 `--audit` 头部措辞分层：主数 `104 of 121`，括注 `53 of those have no anchor at all`；
  `--audit` 逐行给无锚者打 `<- no anchor at all`。
- 删掉 `unanchored and verdict in {"OK","STALE"}` 这道栅栏。T42 为它写的理由是「实测 9 条两者皆是」，
  但赋值落在 `structural is None` 分支内，而 `suffix_ambiguous` —— 通往 AMBIGUOUS 判词的唯一路径 ——
  会跳过整个分支 ⇒ 该栅栏**结构性不可达**（消融实测 red 0）。
- `docs/architecture/durable-runtime-p2-recon.md:67` 的**活体漂移**：它引 `scripts/check-spec-refs.py:250`，
  T42 的头注把那个串推到 277、本票推到 322，**两次守卫都报 OK** ⇒ 改成 `:322`，并注明该行号被守卫判为
  不可校验、每次改守卫都要手工复核。
- `tests/python/test_check_spec_refs.py` 30 → 39（+9）。

## 缺口：两条锚，只有一条看得见行号

| 锚 | 判定 | 看行号吗 |
|---|---|---|
| 符号锚（`best is not None`） | 符号**定义行**是否落在被引范围 | **是**（`offset` ⇒ `STALE`） |
| 内容锚（`content_candidates`） | 被引代码是否在目标文件**出现过** | **否**（文件级） |

## 静态证据：`start`/`end` 全仓只有三个消费者

上界检查（`start > count or end > count` ⇒ `OUT_OF_RANGE`，只看上界）、候选排序里算 `distance`、
以及**唯一**的位置敏感比较 `not (start <= definition <= end)`（置 `offset` ⇒ `STALE`）—— 最后这一处
包在 `if best is not None` 里，所以没有符号锚的引用**永远走不到它**。这是机制，不是读数。

## 分组与两条实验（HEAD `8952b47`）

| 组 | 判据 | 条数 | 把行号改成同范围另一个值，输出会变吗 |
|---|---|---|---|
| P 符号锚 | `symbol is not None` | **17** | **10/17 会变**（阳性对照） |
| W 仅内容锚 | `symbol is None and cc > 0` | **51** | **0/51**（rc 与 stdout 逐字节相同） |
| U 无锚 | `symbol is None and cc == 0` | **53** | 0/53（T42 已测） |

⇒ `LINE-UNCHECKED = best is None` = **104**，且两口径**嵌套**（U ⊂ LINE-UNCHECKED），差集**恰好 51 且全部
`cc > 0`（已落断言，不是人工统计）**。P 组没动的 7 条**恰好就是基线那 7 条 STALE**（突变后仍 STALE，
默认输出不打印 `offset`）—— 账能对上，不是含糊的「大部分」。

## 为什么 v1 探针不能信（自更正记录）

v1 用 `len(spec_text.split("\n"))`（**spec 文件**的行数）当上界，而不是该行自己的 **target** 行数 ⇒
把 291 行的 `step_condition.py` 改到第 833 行 ⇒ 撞 `OUT_OF_RANGE` ⇒ **7 个假 CHANGED**，同时 13/17 被 `skip`。
判据修成 `tc = g._line_count(row["target"])` + `assert 1 <= ns <= ne <= tc`，并**保持区间宽度**（只平移不缩放）。
**教训：阳性对照自己也要过一遍「新值在哪个坐标系里合法」。** 另外 v1 没有阳性对照，「51/51 不可见」当时
有平凡解释（机制根本没生效）—— P 组与每处 diff 恒为 `7 → 8 symbol-stale` 才把它排掉。
另：普查与探针**不能同轮跑**，同轮跑出过污染的 `worker.py:498→499` 读数。

## 消融：5 处突变，打红集合两两不同

M1 `line_unchecked` 反转 → 红 7（私有 `test_the_same_fixture_is_visible_once_a_symbol_is_named`）；
M2 `unanchored` 放宽成 `= line_unchecked` → 红 6（私有 `test_real_corpus_unanchored_census_is_reported`）；
M3 summary 计数回窄口径 → 红 1；M4 `--audit` 不再标「无锚」层级 → 红 1；M5 默认咨询行回窄口径 → 红 1。
两个口径各有**私有红灯** ⇒ 它们被独立钉住，不是同一个谓词的两个写法。
期间发现 M3/M4 一度打红**同一个**测试（一个测试钉了三件事）⇒ 拆成三个。

## 归档：钉值仍在归档提交才移动

`line-endings` `(1848,1839) → (1852,1843)`（4 个文本文件，两数各 +4）；`tasks-refs` `803 → 804`（delta 回到 1：
`implement.jsonl` 只引自己的归档 prd，`check.jsonl` 零条 `.trellis` 路径）。旧值 `(1844,1835)`、`797`
按「不可达值会掩住 -1 漂移」的既有规则**删除**。

🔴 **新增一条实测坑**：`task.py archive` 写出的 `task.json` **没有末尾换行**（T42 那份也一样）。给它补一个裸
`\n` 会产成 `25 CRLF + 1 lone LF` 的**混合文件** —— 而 `git status` 结构上看不见（`autocrlf` 在 add 时抹平）。
是 `check-line-endings.py` 在真实仓上报红抓住的：补的必须是**文件自己用的那个结尾**（`\r\n`）。
修完按字节回读确认 `26 CRLF` 且 `endsNL` true，而不是相信写入。

### Git Commits

| Hash | Message |
|------|---------|
| `59fcf7c` | fix(scripts): report the whole unchecked-line-number class, not just the anchor-free tier (#693) |
| `b1fc293` | chore(task): archive 09-20-t43-line-unchecked-refs |

### Testing

- [OK] `python scripts/check-spec-refs.py` **rc 0**：130 refs / 271 mentions / 114 ok / 7 stale / 9 ambiguous /
  0 structural / 0 content mismatch / 咨询行 **104 of 121**（53 无锚）
- [OK] `pytest tests/python/test_check_spec_refs.py -o addopts=` → **39 passed**（原 30）
- [OK] 全量 `PYTHONPATH=python pytest tests/python/ -o addopts=` → **1243 passed, 10 skipped**（+9 = 本票新增测试数）
- [OK] 消融 5 处突变全部非空、两两集合互不相同；两口径各有私有红灯
- [OK] Scripts CI / Python CI 对 `59fcf7c` 与 `b1fc293` **各自** success
- [OK] 归档同提交内同步钉值后：line-endings `1852/1843` 通过、tasks-refs `804 ok / 0 dangling / 0 malformed`、
  ruff `scripts/` 干净、两个钉值测试文件 **19 passed**
- [OK] `git ls-files` = 1853 ⇒ 1853 − 1 gitlink = 1852 = 1843 文本 + 9 二进制（**两个独立计数相符**）

### Status

[OK] **Completed**

### Next Steps

- 账：`LINE-UNCHECKED` 仍是**咨询**，不进 verdict；要变成结构判据需要先决定「怎么补这 104 条」。
- 账：符号抽取器仍会把路径 token 咬成伪符号（`worker.py:524` → `worker`）；本票只在判据里绕开它，**没有**改抽取器。
- 账：`--basetemp` 指定仓内目录会让守卫的索引源（`git ls-files`）为空 ⇒ 合成语料测试假红；本地不要省这一步。
- 账：本机 `check-tasks-refs-selftest.py` 必中断，只信 CI。
- ⚠️ 复现提示：CRLF 文件里做多行锚点必须用 `"\r\n".join([...])`；**补末尾换行同样要问方向**（本票栽过一次）。



## Session 51: T44 the path predicate was asked twice with two answers — the phantom symbols are one def away from 28 false STALEs (#694)

**Date**: 2026-09-20
**Task**: T44 the path predicate was asked twice with two answers — the phantom symbols are one def away from 28 false STALEs (#694)
**Branch**: `main`

### Summary

T43 的 journal 在「账」里留了一行：符号抽取器仍会把路径 token 咬成伪符号（`worker.py:524` → `worker`），那一票只在判据里绕开它、**没有**改抽取器 —— 本票就是那一行。缺陷的形状是「同一个问题被问了两遍、两个答案」：`_symbols_on` 问「这个 span 是不是纯路径」用的是 `suffix in CODE_EXT`，而 `PurePath("worker.py:524").suffix` 是 `.py:524`、**不在** `CODE_EXT` 里，于是 span 存活并被切成词；`_content_anchor` 问的是同一个问题，只是答案里多了 `PATH_SPAN_RE.match(span)`（T42 刚把 `path[:line]` 教给它）。T42 把理由写下来了，却只修了一边 —— 这正是「代码长出来的说法必须跟着代码走」。危害不是「无害」而是「潜伏」：伪符号只在它恰好是目标文件里的一个 `def` 时才能影响 `best`；在 `5ee7be2` 的语料上这个条件**恰好**不成立（115 个三元组命中 def = 0），所以判词一个都没动，但今天往 `worker.py` 追加一个 `def worker():`，**28 行**从 OK 变成假 STALE。修法 = 把问题收成一个谓词 `_is_path_span`，两个消费者都调它 ⇒ 两边不可能再各自漂移。

### Main Changes

- `scripts/check-spec-refs.py`：抽出 `_is_path_span(span)`（`bool(PATH_SPAN_RE.match(span)) or PurePath(span).suffix in CODE_EXT`），
  `_symbols_on` 与 `_content_anchor` **都改成调它** —— 「这个 span 是不是纯路径」从此只有一处答案。
  两个 docstring 分别记下来龙去脉；`_symbols_on` 那一份还带上本票的普查数字与危害注入读数。
- `line_unchecked` 的注释重写：它原先写的是「抽取器会把路径 token 咬成词」这条**已被本票修掉**的解释
  ⇒ 留着即可，但要说真话（这一层现在的含义是「没有东西可以拿来比行号」）；顺带记下「有名字的行 98 / 两侧分歧 81」。
- `tests/python/test_check_spec_refs.py`：原 test D 原本**建立在伪符号上**（拿 `worker.py` 当「line 2 上没有符号」的样例）
  ⇒ 改写为 bold 形式的 `**never_defined**`（内联 span 一旦带 `_` 会自己变成内容锚）；新增 6 条，覆盖谓词唯一性、
  两侧同表、纯路径产出零符号、真语料口径、以及危害绊线。
- 本票**不新增被跟踪文件**：实现提交里钉值不动，只有归档提交才移动它们（T43 的形状，不是 T42 的）。

## 侦察：五个版本的探针，四次自我更正

侦察的目标只有一个问题：**伪符号到底能不能成为 `best`**。逐版留下的更正（都是「量出来而不是猜」逼出来的）：

- v1 的过滤器把目标形状排除在外、`(spec, line, ref)` 作字典键又吞掉 5 行 ⇒ 整支重写成 v2。
- v2 `RecursionError`：补丁函数里回头调 `m._symbols_on`，而它已被自己替换掉 ⇒ v3 先绑 `_ORIG`。
- **v2 的 62 是低估**：它用 `re.split(r"[._/\-]", …)`，含 `_`；而 `_symbols_on` 用的是 `[.\s()\[\],=:]+`，**不含** `_`
  ⇒ `step_condition` / `graph_store` 根本删不掉。忠实读数是 88（v3）/ 89（v4）。
- 我自己的散文一度与自己的数据矛盾（写「v2 也会删裸文件名」，而打印出来的行显示 v2 什么都没删）
  ⇒ 微探针实测 `PurePath('step_condition.py:1-26').suffix == '.py:1-26'`，真因是**切分字符类不一致**，不是粗体/裸名处理。
- v5 的散文写「这条理由不承重」，而它自己数出来的是 **17** ⇒ 更正为「承重」（且修完之后这句话不再成立，故一并删掉）。

## 普查：三个口径必须一起读

「伪符号」有三种合理定义，数目不同，**只有一个结论是三者共有的**：

| 口径 | 定义 | 三元组 | 词种 | 命中 def |
|---|---|---|---|---|
| ① | 当时脚本内联的「mirror」读法 | 记录值 115 | 15 | **0** |
| ② | 减法口径：把所有含路径 token 的 tick span 整段删掉后**仍抽得到**才不算 | **115** | **14** | **0** |
| ③ | 最宽：token 出现在 span 内即算（相减之前） | **122** | **16** | **0** |

- 本次**从修前提交 `5ee7be2` 的 blob 重新算过 ②**（不是引用当时的汇总）：115 三元组 / 14 词种 / **命中 def = 0** /
  **89 of 130** 行带伪符号（其中 80 行有 target）—— 与 docstring 逐字相符。`worker` 一个词占 28 条，
  与「注入 `def worker` 打红 28 行」是**同一个 28**。
- ③ 是**绊线测试用的口径**（`122` 与 `hits == []` 都钉在测试里，非空且带正对照）—— 它比 ② 宽，
  所以绊线不会因为口径收窄而变成装饰。
- ① 与 ② 总数相同但**组成不同**（各 5 行，`t44_attr.log` 留了两串具体行）；① 的口径没有留脚本 ⇒ 见「账」。

三者共有的结论：**命中 def 恒为 0** ⇒ 判词一个都没动，修法对判词不可见。而这是**偶然**的，不是结构性安全。

## 修法：一个谓词、两个消费者

- 唯一的**行为变更**在 `_symbols_on` 侧；`_content_anchor` 侧等价（它本来就含 `PATH_SPAN_RE`）。
- 验收：`114 ok / 7 stale / 9 ambiguous / 0 structural`、带 symbol **17**、UNANCHORED **53**、LINE-UNCHECK **104**
  —— 与修前**逐项相同**；变的只有「有名字的行」98 与「两侧分歧」81 这两个咨询口径。
- 谓词本身有两个半边，各有一处突变能把它打红（见下），所以「一条谓词」不是把两处判断合并成一处的说法文章。

## 危害是潜伏的，不是无害的（两态对照，今天在 HEAD 上重跑）

| 守卫 | 注入前判词 | 注入后**字段级变化行** | 注入后判词 |
|---|---|---|---|
| 修前（`5ee7be2` 的 blob，52567 字节，sha `b70d7ba7…`） | 114 OK / 7 STALE / 9 AMBIG | **28** | 86 OK / **35 STALE** / 9 AMBIG |
| 修后（工作树） | 同上 | **0** | 114 / 7 / 9（不变） |

注入 = 往 `python/ultimate_coders/agent/worker.py` 追加 `def worker(): return None`。
前 3 条变化示例：`agent-capability-spec.md:57 / 71 / 574` 都变成 `('STALE', 'worker', 2223)` ——
**一个 `def` 换来 28 个假 STALE**。修法**卸掉**了这个危害，不只是绕开它。
收尾：注入后 `worker.py` sha `e63b69ee…`；复原后**另一进程**复算 `55eb36de…` = 基线；守卫 sha 未变。

## 消融：M1 ⊊ M2

- **M1**（把 `_symbols_on` 侧的排除动作退回窄问题 `suffix in CODE_EXT`）→ 红 **5** 条；
  **M2**（把共享谓词里的 T42 那一半拿掉）→ 红 **16** 条；**M1 − M2 = ∅**（M1 的红全是 M2 的子集）。
- 形状与 T43 的教训一致：**宽破坏的红集合必须包含窄破坏的**，否则说明窄的那处没被真正钉住。
- 两处都按字节复原，并由**另一进程**复算 sha256 回 `1caf167a…`。

## 归档与钉值

- 归档提交 `d48edaf`（6 文件，+132/−8）**自带**钉值同步：`check-line-endings` `(1852,1843) → (1856,1847)`
  （4 个新文本文件，两数各 +4）、`check-tasks-refs` `(803,804) → (804,805)`。
- 实现提交 `194793b`（2 文件，+307/−14）不新增被跟踪文件 ⇒ 两处钉值都不动。
- `task.py archive` 的老账仍在：写出的 `task.json` **没有末尾换行**，补的必须是**文件自己用的那个** `\r\n`
  （补一个裸 `\n` 会产成混合行尾文件，而 `git status` 结构上看不见）。

## 顺带发现：`index.md` 里 Session 49 那行是错位的

- `| 49 | 2026-09-20 | T42 … (#692) |` 落在 `## Notes` **之后**，而表体在 `<!-- @@@auto:session-history -->`
  与闭合标记之间 ⇒ 表里 50 有、**49 没有**。
- `add_session.py` 的 `update_index` **没有**任何能在标记区以外写一行的代码路径（唯一的行写入在标记区内）
  ⇒ 这不是工具产物，是历史上手工或旁路脚本改的。
- 本次归位（一行移动）。检查器不钉它（`check-journal-ledger` 只对 `Total Sessions` 给咨询），
  但在这个文件里读表的人会漏掉一整票 —— 所以它不是纯格式问题。


### Git Commits

| Hash | Message |
|------|---------|
| `194793b` | (see git log) |
| `d48edaf` | (see git log) |

### Testing

- [OK] `python scripts/check-spec-refs.py` **rc 0**：130 refs / 271 mentions / **114 ok / 7 stale / 9 ambiguous** /
  0 structural failure；咨询行 **104 of 121**（53 无锚）—— 与修前逐项相同
- [OK] `pytest tests/python/test_check_spec_refs.py -o addopts=` → **45 passed**（原 39）
- [OK] 聚焦三文件（spec-refs + line-endings + tasks-refs）→ **64 passed**
- [OK] 全量 `PYTHONPATH=python pytest tests/python/ -o addopts=` → **1249 passed, 10 skipped in 66.32s**
  （+6 = 本票新增测试数）
- [OK] 消融 M1/M2 **今天在 HEAD 上复跑**：M1 红 5、M2 红 16、**M1 − M2 = ∅**、两处均跨进程复算 sha 回 `1caf167a…`
- [OK] 危害两态对照 **今天复跑**：修前守卫（`5ee7be2` 的 blob）字段级变化 **28 行**（114/7/9 → 86/35/9）；
  修后守卫 **0 行**；注入后 `worker.py` sha `e63b69ee…`、复原后另一进程复算 `55eb36de…` = 基线
- [OK] 修前口径复算（本次从 `5ee7be2` 的 blob 重算 ②）：**115 三元组 / 14 词种 / 命中 def = 0 / 89 of 130 行**
- [OK] 三个守卫：journal-ledger（50/50 会话、0 占位符）、line-endings（1856 / 1847 / 9 / 1）、
  tasks-refs（805 ok / 0 dangling / 0 malformed）全 rc 0
- [OK] Scripts CI / Python CI 对 `194793b` 与 `d48edaf` **各自** success
- [OK] 归档提交内同步钉值：line-endings `(1852,1843) → (1856,1847)`、tasks-refs `(803,804) → (804,805)`；
  两个钉值测试文件 **19 passed**

### Status

[OK] **Completed**

### Next Steps

- 账：`LINE-UNCHECKED` 仍是**咨询**，不进 verdict；要变成结构判据得先决定「怎么补这 104 条」。
- 账：①（内联「mirror」口径）与 M1/M2 的数字都出自当时的工作树 —— M1/M2 的脚本留在
  `.workbuddy/tmp/t44_ablate.py` 且今天已复跑；**① 的口径没有留脚本**，只剩 `t44_attr.log` 里的行列表
  ⇒ 要么留脚本，要么别引用。
- 账：`update_index` 的第二个副产品：`splitlines()` + `"\n".join()` 写回会**吞掉文件末尾换行**
  （本次补回，方向按文件自己的 `\r\n`）；同一函数的行写入只发生在标记区内，所以 EOF 那行错位**不是**它干的。
- 账：伪符号的**来源**已封死（不再产生），但「混合 span」（`` `Task.to_dict` (`types.py:67-80`) `` 这种
  既含真符号又含路径）仍按混合处理 —— 本期只保证纯路径 span 不再被咬成符号。
- ⚠️ 复现提示：`v4/v5` 探针把「当前 `_symbols_on`」当修前基准 ⇒ 修复落地后重跑只会得 0；要复算修前口径，
  必须从 `git cat-file blob 5ee7be2:scripts/check-spec-refs.py` 取一份守卫副本。
- ⚠️ 复现提示：CRLF 文件里做多行锚点必须用 `"\r\n".join([...])`；补末尾换行同样要问方向。


## Session 52: P2 runtime policy baseline — verify, commit, archive

**Date**: 2026-09-22
**Task**: P2 runtime policy baseline — verify, commit, archive
**Branch**: `main`

### Summary

Verified the uncommitted P2 baseline end-to-end, committed it as one work commit, and archived the task.

### Main Changes

## Session 52: P2 runtime policy baseline — verify, commit, archive

**Date**: 2026-09-22
**Task**: `09-20-p2-runtime-policy` (now `archive/2026-09/09-20-p2-runtime-policy`)
**Branch**: `main`

### Summary

The P2 baseline implementation (written in a prior session, left uncommitted)
was verified end-to-end against its spec, committed as one work commit, and
the task archived. This session wrote no production code: the only edits are
the verification note in the task's `research/notes.md` and the `commit`
pointer in the archived `task.json`.

### Main Changes

- Re-ran every P2 acceptance check on the dirty tree; all green (see Testing).
- Independent spec review (read-only subagent) over the full diff vs
  `runtime-policy-spec.md` + `prd.md`/`design.md`: all three scenarios PASS,
  no wrong-vs-correct violations, no over-claims.
- Work commit `d747b563` (17 files, +1198/−20): runtime report, review
  verdicts, capacity placement, spec pointers, P2 architecture doc.
- Archive commit `bb9e016e` via `task.py archive` (auto-commit).
- Pre-existing failure recorded, not repaired: `cargo clippy -D warnings`
  fails on `crates/uc-grpc/src/server.rs:3717` (`let_unit_value`) — that file
  is outside this task's diff.

### Git Commits

| Hash | Message |
|------|---------|
| `d747b563` | feat(runtime): P2 baseline diagnostics, review verdicts, capacity placement |
| `bb9e016e` | chore(task): archive 09-20-p2-runtime-policy |

### Testing

All re-run today on this tree (nothing copied from prior notes):

- [OK] `.venv pytest tests/python/test_review_policy.py tests/python/test_workflow_orchestration.py` → **72 passed** (`review.py` 100% coverage in the report)
- [OK] `cargo test -p uc-engine --lib runtime_metrics` → **3 passed**
- [OK] `cargo test -p uc-grpc --lib` → **226 passed** (placement filter 30, no affinity regressions)
- [OK] `cargo check -p uc-grpc-server` → clean
- [OK] `cargo check -p uc-engine --no-default-features --features storage --example runtime_report` → clean
- [OK] `cargo test -p uc-engine --test runtime_report_integration --no-run` → compiles (live-PG test stays `#[ignore]` by design; no DB claimed)
- [OK] `cargo fmt --check` → clean; `ruff check` on review/worker/sandbox/tests → clean
- [FAIL, pre-existing] `cargo clippy -p uc-engine -p uc-grpc -- -D warnings` → one error at `server.rs:3717`, file untouched by this task

### Status

[OK] **Completed**

### Next Steps

- Next architecture slice TBD — P2 doc's deferred follow-ups: adaptive
  optimization (needs objective + failed-attempt usage), strict review
  independence (admission/delivery enforcement), repair loops (versioned graph
  expansion), monetary market (bids/budgets).
- Optional: fix the pre-existing clippy `let_unit_value` at `server.rs:3717`.


### Git Commits

| Hash | Message |
|------|---------|
| `d747b563` | (see git log) |
| `bb9e016e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
