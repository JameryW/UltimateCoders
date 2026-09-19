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

### Git Commits

| Hash | Message |
|------|---------|
| `adb33ae` | `ci(workflows): guard the README CI prose count and the footnote's two claims (#688)` — 4 files, +219/−14（守卫 +113 / 测试 +92 / workflow 头注 +7−7 / 语料钉值 +14−4） |
| `d8cac38` | `chore(task): archive 09-19-t38-guard-readme-ci-prose` — 4 files, +206（含 `task.json`，归档提交按仓规带上） |

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

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`adb33ae` 实现 + `d8cac38` 归档 + 本会话的账本提交）；`adb33ae` 上三套 CI **全绿** ⇒ **#688 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：**守卫自己的对账口径也是前提，也要被检查**。T37 的 `truth = paths - {自身}` 把一句脚注**当成了公理**；本票把它变成判据 9。⇒ 写守卫时问一句：**我算这个集合时，减掉的/加上的东西，是我检查过的，还是我假设的？**
- **本票付的学费（写进技能）**：① **两方向消融**（旧守卫全绿 / 新守卫全红）比单向突变强 —— 它同时证明「缺口真实」与「修的就是这个缺口」；② **覆盖断言自身也要消融**，否则它可能本身就是自指断言；③ **绝对量兜底**在「备份已被污染」时是唯一可用的恢复依据（同一次运行里取的哈希无法自证）；④ 计数解析器**必须**把「找不到」当失败。
- **账 5（本票新增）**：§ CI 段 `Checks` 列的散文数字、job 数、测试基线数**仍无守卫** —— 待决。
- **账 4（延续）**：`add_session.py` 的个人 index 行数估值偏低（取自填充前），下次运行自愈；`journal-2.md` 满 2000 行 ⇒ 本会话起用 `journal-3.md`。
- **账 2（实测）**：`ruff format --check` 有 **76 个文件**待改 ⇒ **大票**，延后。
- **账 1（延续）**：`paths` ↔ `Cargo.toml` 一致性**仍无守卫**（需 CI 有 Rust 工具链 + 先裁对账口径，仓内无法坐实）。
- 延续：`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。
