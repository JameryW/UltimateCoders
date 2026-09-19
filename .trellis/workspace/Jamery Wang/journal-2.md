# Journal - Jamery Wang (Part 2)

> Continuation from `journal-1.md` (archived at ~2000 lines)
> Started: 2026-09-16

---



## Session 27: T24: #673 切片 B —— 把 50 处「符号优先」引用从 path:line 改成 path（可去行号 52 → 51）

**Date**: 2026-09-16
**Task**: T24: #673 切片 B —— 把 50 处「符号优先」引用从 path:line 改成 path（可去行号 52 → 51）
**Branch**: `main`

### Summary

T24 #673 切片 B：判定从「存在性」升级为「唯一性」⇒ 148 条里合格 51 条，形状闸门再扣 1 条 ⇒ 重写 50 处 / 9 文件，refs 148 -> 98、mentions 221(153/47/21)，零覆盖损失逐条断言、存活 98 条 verdict 全不变、exit 0；预 26 条 STALE 里 25 条本身就是漂移的行号。同票修守卫两处已测量的小缺陷（bold 锚 identifier-exact、EXCLUDE_DIRS 加 .scratch，各恰好影响 1 行 / 1 条判定）。

T24 #673 切片 B：把**可机械判定为安全**的引用真的去掉行号。判定条件从**存在性**（本行有一个锚）升级为
**唯一性**（剩下的锚仍能定位到**一处**）⇒ 148 条里合格 **51** 条（42 符号锚 + 9 唯一内容锚），
再加一道**形状闸门**扣下 1 条 ⇒ **重写 50 处、9 个文件**，`refs 148 -> 98`、
`mentions 171(102/47/22) -> 221(153/47/21)`。**零覆盖损失是逐条断言的**（不是抽样），
存活 98 条 verdict **全不变**、守卫 **exit 0**、**0 structural**。顺带修掉守卫两处**已测量**的小缺陷，
每处全仓影响**恰好 1 行 / 1 条判定**。实际收益：pre 的 **26 条 STALE 里 25 条本身就是漂移过的行号**。

### Main Changes

**起点：84 → 52 → 51，每一次都被自己的测量推翻。** T22 量出「84 / 148 今天就能去行号」，
那是**存在性**口径；T23 加**唯一性**后修正为 52；本票又测得 **51** —— 差额那一条正是
`error-handling.md:307`，它当时的唯一锚是**散文**里的 `delete`（来自
`**Mapping \`MemoryWriteError\` for delete operations**`）。把 bold 收紧成 **identifier-exact**
之后该行只剩「路径 span（按设计被排除）+ 无下划线的 `MemoryWriteError`」⇒ **确实零锚**。
⇒ **这是修正不是损失**：若照旧重写，它剩下的证据是**零**。三处记录（`prd.md`、`#673`、`topics/`）已同步。

**形状闸门：唯一的扣下理由，恰好两个独立理由落在同一行。** 提及扫描只读**反引号 span** 且**围栏之外**
⇒「去行号后守卫还看得见吗」是**可判定**的。实测**恰好 1 条**不合格：
`error-handling.md:320` 的 `sandbox.py:1007/1546` 是全文**唯一裸写**（无反引号）**且**唯一 `/双行号` 形态
（机械删除会产出 `sandbox.py/1546`，`MENTION_PATH_RE` 不匹配）。⇒ 扣下是唯一正确处置；
要真修它得重写那句散文，属非目标。

**干跑不能用影子树（一次被实测推翻的设计）。** 首版把 `.trellis/spec` 拷进临时树并把 `ROOT` 一起指过去，
得到 **216/221 目标解析不到**：守卫靠**真仓库**解析（`_repo_index()` 走 `ROOT`、`_line_count` 读 `ROOT/rel`、
`collect()` 里 `spec.relative_to(ROOT)`）。放仓外 ⇒ `relative_to` 直接炸；放仓内 ⇒ 自己的 `.md` 进索引。
⇒ 改为：干跑只预测**形态**，真证明在**写盘之后**跑真守卫，任一断言失败按**字节**回滚。

**收益分解：26 条 STALE 里 25 条本身就是漂移过的行号。** `114 ok / 26 stale` → `89 ok / 1 stale`，
25 条各就各位 ⇒ 本票的实际效果是「漂移的行号从**看起来权威**变成**没有行号**」。
`agent-capability-spec.md:603` 那对同行两引用**一 OK 一 STALE** 的原因：`_execute_steps` /
`_run_single_step` 对 `start=1053` 与 `start=1246` 的**最近距离不同**。
`index.md` 重写后剩 0 条引用 ⇒「有引用的 spec 文件」**12 → 11**（守卫首行输出即 11，不是笔误）。

**守卫自身两处已测量的缺陷，同票修掉。**
① bold 锚收紧为 identifier-exact ⇒ 全仓**恰好改变 1 行**（`STALE 27 -> 26`、`OK 113 -> 114`）；
② `EXCLUDE_DIRS` 增加 `.scratch` —— **判定来源必须可复现**：`os.walk` 不看 git，
`.scratch/pt-test_*/**/lib.rs` 把 `lib.rs` 的候选从 **4** 抬到 **79**；排除后全仓**恰好 1 条判定**变化
（`event-pipeline-spec.md:153` `dashboard/app.py`：ambiguous → resolved）、**0 条 ref 判定**。
**正向副作用**：这让「仓内影子树 + 真 ROOT」这一**此前不可用**的测量手法变得可用 —— 上面的分解就是用它跑的。

**三个数字同源，不是三处不一致**：重写**发生 50 次**、消失的键 **49** 个、`numstat` **49** 行 —— 差额全来自
`agent-capability-spec.md:603`（一行两引用**且两名同名** ⇒ 键与 git 行都塌成 1）。
`assert len(after) == len(rows) - len(plan)` 用的是**列表长度**，不会因塌键误判。

### 修正与自纠（三处，都是我的错）

1. **`MemoryWriteError` 我读错了。** 我断言「它含 `_` ⇒ `CONTENT_TOKEN_RE` 应接受它 ⇒ `cand=0` 是缺陷」。
   逐条件打印后：`search('_','MemoryWriteError') == False`、`search('_','A_B') == True` ——
   **该标识符是 CamelCase，没有下划线**。同行路径 span 被排除是**设计**（`PATH_SPAN_RE`）。
   ⇒ **守卫是对的，我错了**；又一次「从字面量推断而非读字节」。
2. **我自己的回滚副本污染了我自己报出的数字。** 首跑报 `151 resolved`，真相 **152** ——
   备份放在 `.scratch/t24-backup`（**在仓内**），守卫 `os.walk` 看得见它 ⇒
   `event-pipeline-spec.md:153 type-safety.md` 从 resolved 变 ambiguous。⇒ 备份改到**仓外**
   （`%TEMP%/t24-backup`）+ 守卫排除 `.scratch`，并把「备份不得留在被审计的仓内」写进工具注释。
   **与 T23 的影子树同型：工具扰动它正在测量的对象。**
3. **消融脚本里的手写字面量。** 首版把 `EXCLUDE_DIRS` 两行按 `\n` 拼成字面量，而该文件是 **CRLF**
   ⇒ 断言 `count(...) == 1` **在写盘前**当场炸（符合「assert 先炸再写盘」）。改为**从文件字节推导**突变。

### 已知局限（如实记账）

- **41 / 50 仍能说出一个定义在目标文件里的符号**；另 **9 条**现在**只**靠路径解析 ⇒ 去掉行号后
  它们**不再有任何「指向某处」的证据**。这是「符号优先」口径的已知代价，也是 #675 切片 B/C 的输入。
- **97 条引用仍带行号**（32 内容锚歧义 + 65 无锚）⇒ 行号照旧会漂，符号优先**不是**全量解法。
- **扣下的 1 条仍在**（`error-handling.md:320`）—— 它是「看得见的问题」，不是静默消失。
- 判定仍**零 CI 覆盖**：`scripts/**` 与 `.trellis/**` 不在任何 workflow 的 `paths` 内
  ⇒ 本票与 T21/T22/T23 一样**零 CI 触发**。接 CI 会改变触发面（`.trellis/**` 一改就跑 Python CI），
  属 **#673 切片 C** 的独立决策。


### Git Commits

| Hash | Message |
|------|---------|
| `4ff5f78` | (see git log) |
| `7ff0a66` | (see git log) |

### Testing

- **重写后跑真守卫**（`python scripts/check-spec-refs.py`）：`98 refs / 11 spec files`、
  `221 mentions = 153 resolved / 47 dangling / 21 ambiguous`、
  `89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural`、**exit 0**。
  对比 T23 基线：refs `148 -> 98`（50 处重写）、mentions `171(102/47/22) -> 221(153/47/21)`。
- **零覆盖损失 = 逐条断言（非抽样）**：每条重写后的引用都必须 ① 以提及被守卫看见（`MENTION_OK`）、
  ② 解析到**同一个**目标文件、③ 保留原有 verdict；另断言存活 98 条**逐条 verdict 不变**。
  `python .scratch/t24-rewrite.py --apply` 内建这三条断言，任一失败即从**仓外**备份按字节回滚。
- **恢复的字节精确性另证一次**：`t24-restore.py` 写回 9 个文件后 `git status` 只剩守卫一处修改、
  `git diff --numstat -- .trellis/spec` 为空；`backend/index.md` 恢复后 sha256[:16] = `f87d1f6a3e56e640`
  —— **与 T23 记录里的基线 sha 相同**（`HEAD:` 的 LF 版转成 CRLF 后同哈希）⇒ 三方一致。
- **改动粒度**：`git diff --numstat` = **49/49**（9 文件），不是整文件重写 —— 50 处重写落在 49 行上。
- **消融 T0/T1/T2**（`python .scratch/t24-ablation.py`，三跑都走真 CLI 子进程）：T0 提交态 `153/47/21` exit 0；
  T1 把 `.scratch` 一处突变 ⇒ `152/47/22` exit 0（**T1 != T0** ⇒ 子句在起作用）；T2 按字节恢复、
  sha256 `7f4d983b91de8f03` 一致、输出 **== T0**；**三次 exit 全 0**（未松动门禁）。
  refs 三项在 T0/T1 完全相同 ⇒ 该子句只触及 advisory 面。
- **独立复算**：分类与形状闸门（`.scratch/t24-rewrite.py`）与守卫两路一致；
  pre-rewrite 分解（`.scratch/t24-whatif.py`，用 `git show HEAD:` 重建 31 个 spec 文件到**仓内**预览树）
  算出「26 条 STALE 里 25 条被重写」—— 该手法**只有**在 `EXCLUDE_DIRS` 排除 `.scratch` 之后才可用。
- **lint**：`.venv/Scripts/ruff.exe check scripts/check-spec-refs.py` → `All checks passed!`。
  ⚠️ `ruff format --check` 报 `1 file would be reformatted`，但**改动前后都报** —— 本轮**直接取证**
  （把 `HEAD~2:` 的版本导出到 `.scratch/t24-guard-at-HEAD.py` 再跑）确认是**既有状态**，
  故**不**整文件重排（会引入约百行无关噪声）；`scripts/` 也不在 CI 的 lint 面内。
- **CI**：**零触发** —— `scripts/**`、`.trellis/**` 不在任何 workflow 的 `paths` 内。

### Status

[OK] **Completed**

### Next Steps

- **#673 切片 C**：决定守卫要不要接 CI（接线会改变触发面：`.trellis/**` 一改就跑 Python CI）。
  本票把 **97 条**引用留在原地（32 内容锚歧义 + 65 无锚）⇒ 它们的行号照旧会漂。
- **#675 切片 B**：47 处悬空提及逐条分类处置（真过期的改 spec；仓外 / 运行时 / 示例的**带理由豁免**），
  **不臆造**；切片 C 再谈门禁化，并决定要不要给守卫加**合成语料**的 pytest（别断言本仓计数）。
- **#673 派生**：**9 条**引用现在**只**靠路径解析（不再有指向某处的符号证据）—— 要补锚即 #675 切片 B/C。
- **#674**（仍开放，本票补三条一手测量）：
  ① `journal-1.md` 仍有 **18 处**模板占位符，分布在 **18 个** session（1–15 / 19 / 20 / 21）；
  ② 按「Testing 段**只剩**占位符」的口径是 **14 个** session（1–11 / 19 / 20 / 21，与 #674 原记录逐字一致），
  余 **4 个**（12–15）是**有作者正文、只残留模板占位符** ⇒ 两个数字都对，**口径不同**；
  ③ 计数必须**按标题位置切片**：用 `re.split('^## Session \\d+:')` 会因 Session 13 的重复标题错位，
  把 18 数成 19（本票实测踩过一次）。


## Session 28: T25: #675 切片 B —— 47 处悬空提及逐条分类，7 处真修 + 40 处带理由豁免（并推翻「tui 是另一个仓库」的定性）

**Date**: 2026-09-17
**Task**: T25: #675 切片 B —— 47 处悬空提及逐条分类，7 处真修 + 40 处带理由豁免（并推翻「tui 是另一个仓库」的定性）
**Branch**: `main`

### Summary

#675 切片 B 交付：47 处悬空提及按一手证据划成 7 个完全不相交的类；7 处真过期改 spec，40 处进入守卫新增的 MENTION_EXEMPT/SUBJECT_REMOVED 带理由豁免（含「整篇豁免须由 spec 自己点名删除提交」与「拒绝裸通配符」两条自检）。47 → 40 悬空、0 unclassified、exit 0；既有 ref 判定 89/1/8/0 一格未动。**推翻票面定性**：tui/ 不是「另一个仓库」，而是本仓 d7f4631 删除的子系统。

### Main Changes

**范围**：#675 **切片 B**。切片 A（T23 `848b1c7`）让守卫**看得见**「无行号路径提及」这一类（advisory、恒不失败）；本票负责把实测的 47 处逐条**分类并处置**。

**一手证据推翻了票面定性。** #675 与守卫 docstring 都称 `tui/**`（27 处）描述「**另一个仓库**」，据此归入「仓外 ⇒ 合法豁免」。实测证伪：`git log --diff-filter=A -- 'tui/*'` 显示 `tui/` 曾被本仓跟踪；`git ls-tree -r HEAD | grep '^tui/'` 为空；删除提交是 **`d7f4631`**（2026-06-25, #157），正文逐字写着 *"Delete tui/ directory (Ink/React TUI no longer needed)"*（84 files / **−15336**）；替代物 `packages/uc-orchestrator/src/**` **在仓内**；而 `git show d7f4631 --name-only | grep tui-grpc` **为空** —— spec 从未同步，`tui-grpc-spec.md` 最后改于 2026-06-23，**早于**删除。
⇒ 定性改为「**本仓已删、spec 未同步**」，处置从「豁免」改为「**加状态横幅**」。**「间接信号 ≠ 一手事实」的又一次实例：票面写下的定性自带权威感，反证就在 `git log` 里。**

**7 类划分完全且不相交**（脚本内 assert）：`{29, 2, 4, 5, 1, 4, 2}` = **47**，**未分类 0**。真过期 **7 行**改 spec：`docker.rs` 从「当前目录」表移除（`514ec3b` 已删它）；两处 `test_agent.py` 换成真实的按组件分文件（`47f2add` 之后 `tests/python/` 已无那个文件，也再无 `TestTask`/`TestOrchestrator` 这些类）；`nats-bridge-spec` 测试小节重指真实文件；两处 Python scheduler 重指 `crates/uc-python/src/scheduler.rs`（`15b5ae3` / #548）。

**两处比票面更严重，只换路径会写成「半真」**：① `nats-bridge-spec.md:353` 不只两个文件失效 —— 该小节 9 个测试名在 `tests/python/` 里**一个都不存在**（松匹配只命中另一个名字），整段过期；② `dashboard-spec.md:116` 文档化的 `trigger_job` 在 `PySchedulerService`（`crates/uc-python/src/scheduler.rs`）上**不存在**，且 `Orchestrator.scheduler` 永为 `None` ⇒ dashboard 的 trigger 端点恒 503。故写成「**已移除**」并写清现居地，「不臆造」。

**守卫加带理由的豁免机制**（40 行进入，40 == dangling）：`MENTION_EXEMPT` 细粒度 `(spec, ref-glob, reason)` + `SUBJECT_REMOVED` 整篇级。关键设计：**整篇豁免只有 spec 正文点名删除提交时才被承认**（横幅不可能被忘掉）。`exemption_self_check` 三条判据：① 规则必须命中 ≥1 条（死规则 = 对语料的谎报）；② 横幅必须存在且该 spec 仍有悬空提及；③ pattern 不得是**裸通配符**。输出把 dangling 拆成 `exempt` / `unclassified`，按理由分组列出。**exit code 不变**（提及仍 advisory，门禁决策归切片 C）。

**判据 3 是实测逼出来的，不是设计出来的。** 消融 M4 把一条细粒度规则的 pattern 从 `new_module/impl.rs` 放宽成 `*`：它**静默变成整篇豁免**，注入的无关提及被一并吃掉，而自检**一个问题都不报**、`--audit` 仍报 `0 unclassified` —— 一份**看起来完全健康**的输出。当场补判据 3，重跑后 M4 报 `self_check_problems=1`。**只数命中数的检查器，看不见「一条规则比它的理由更宽」。**

**消融六格**（一次一处突变，恢复后校 sha256；数字读自守卫 stdout，不重算）：M0 clean → M1 删横幅令牌 = **27 unclassified + 1 自检问题** → M2 死规则 = 4 + 1 → M3 注入真悬空提及 = 1 unclassified（**不被吞**）→ M4 放宽成 `*` = 被吞**且被报** → M5 与 M0 **逐字节相同**。**六次运行 exit 全 0**，advisory 语义未被削弱。

**数字**：`--audit` **exit 0**、`0 structural`；`47 → 40` 悬空、**40 == exempt**、**0 unclassified**；提及 `221 → 227`（+13 resolved / −7 dangling，即修复引入的真实指针多于删掉的死提及）；三方对账 `166 + 40 + 21 = 227`，**stdout 与 `--json` 两条路径一致**。**既有 ref 判定一格未动**：`89 ok / 1 stale / 8 ambiguous / 0 structural`，与 T24 收口**逐项相同**。7 处修复逐条验证（死提及 `gone` / 活指针 `resolves` / 仍悬空 **0**）。

**门禁**：`cargo fmt --all --check` clean（Rust 未动）；`ruff check` clean；`ruff format --check` **改动前后都失败**（用 `git show HEAD:scripts/check-spec-refs.py` 取证 —— 既有的 `EXCLUDE_DIRS`/`CODE_EXT` 两块就是不许被格式化的；`scripts/**` 不在 CI lint 面内）。9 篇 spec 的 `git diff --numstat` 全为小改动（最大 `10/17`，来自被整段替换的 20 行块），改动后**孤立 LF 全 0**。

**两处自我更正**：① 首版我用 `not r['target']` 过滤得 **68**，与守卫自报的 47 不符 —— 读源码确认判据是 `verdict == DANGLING`（**candidates 为空**），多出的 21 条是 `MENTION_AMBIGUOUS`（target 为 None 但候选非空）。**先复核判据，再复核数字。** ② 本轮注入的 `current_time` 是 `2026-09-16 22:17`，而 `date` 实测 `2026-09-17 17:15 +0800`、HEAD 提交时间戳是 `2026-09-16 23:14 +0800` ⇒ **注入值早于已完成的提交**，必为陈旧信号；任务目录与日期以 `date` + git 时间戳为准。

**提交**：`868cc92`（实现，13 files / +570 −39）+ `d96f071`（归档 + 验收表回读修正）。**#675 保持 open** —— 切片 B 已交付，**切片 C**（是否升级为门禁 / 是否接 CI / 是否加合成语料 pytest）未做。


### Git Commits

| Hash | Message |
|------|---------|
| `868cc92` | (see git log) |
| `d96f071` | (see git log) |

### Testing

- 消融自检（`.scratch/t25-ablation.py`，一次一处突变、恢复后校 sha256；数字读自守卫 stdout 而非重算）：M0 干净 / M1 删掉 `tui-grpc-spec.md` 横幅里的 `d7f4631` → **27 unclassified + 1 自检问题** / M2 把 `uc.scheduler.yaml` 规则改成匹配不到的形状 → 4 + 1 / M3 在围栏外注入 `totally_missing_zzz.py` → 1 unclassified（**不被吞**） / M4 把细粒度规则放宽成 `*` → 被吞**且被报**（判据 3 即由此补上） / M5 与 M0 **逐字节相同**。**六次运行 exit 全 0**，advisory 语义未被削弱。
- 逐条验证 7 处修复（`.scratch/t25-verify.py`）：死提及全部 `gone`、活指针全部 `resolves`、仍悬空的 7 处锚点为 **0**。
- 收口验收（`.scratch/t25-accept.py`）**8/8 PASS**：exit 0、`structural=0`、`unclassified=0`、`exempt == dangling == 40`、`166+40+21 = 227`（stdout 与 `--json` 两条路径一致）、ref 判定 `89 ok / 1 stale / 8 ambiguous / 0 structural` 与 T24 收口逐项相同。
- 门禁：`cargo fmt --all --check` clean（Rust 未动）；`ruff check scripts/check-spec-refs.py` clean；`ruff format --check` **改动前后都失败**（用 `git show HEAD:` 的版本取证 = 既有状态，且 `scripts/**` 不在 CI lint 面内）；9 篇 spec 的 `git diff --numstat` 全为小改动（最大 `10/17`），改动后孤立 LF 全 0。

### Status

[OK] **Completed**

### Next Steps

- **#675 切片 C**（本票未做，票因此保持 open）：是否把提及升级为门禁、是否接 CI、是否加合成语料 pytest。⚠️ 接 CI 是**触发面**变更 —— `.trellis/**` 一旦进 workflow 的 `paths`，该目录上的改动就会开始跑 Python CI。
- **判据 3 的残余风险**：它只挡**裸通配符**，不挡「比理由更宽的模式」（`tui/**` 这类合法 pattern 同样能覆盖整个 spec 的悬空集）。切片 C 决定门禁之前需先处理这条。
- **两篇已删子系统的 spec 正文未订正**（只加了横幅）：逐节重写需每节重新取一手证据，建议另开一张票。


## Session 29: T26: #675 切片 C —— 豁免表加判据 4 + `scripts/**` 首次进入 CI（第一次 CI 打红暴露的其实是索引源缺陷）

**Date**: 2026-09-17
**Task**: T26: #675 切片 C —— 豁免表加判据 4 + `scripts/**` 首次进入 CI（第一次 CI 打红暴露的其实是索引源缺陷）
**Branch**: `main`

### Summary

T26 = #675 切片 C：给 `MENTION_EXEMPT` 加**声明命中数**并在自检里对账（判据 4），把 T25 记为残余风险的「非裸但宽于理由的 pattern」机械封死；消融证明判据 3 与 4 打红的集合**不相交**（T19 判据）、exit 仍恒 0。同时让 `scripts/**` **首次进入 CI**（新 `ci-scripts.yml`，无 `paths` 过滤、矩阵 3.9+3.12；代价侧由首次运行 23s 实测背书）。🔴 **第一次 CI 同时打红两档，暴露的是真缺陷**：守卫的 `os.walk` 索引看得见本机被 gitignore 的文件 ⇒ **同一个提交在本机与干净检出给出两个判词**；修法换根因 —— 索引改为 `git ls-files ∩ EXCLUDE_DIRS`（爆炸半径实测 1/325，C\W = 0）。终局数字：`89/1/8/0` 一格未动、`mentions 41 of 227`（0 unclassified）、收集 1149 → 1160、两档 CI 全绿。

### Main Changes

**范围**：#675 **切片 C**（#675 的最后一个切片）。切片 A（T23 `848b1c7`）让守卫**看得见**「无行号路径提及」；切片 B（T25 `868cc92`）把 47 处逐条分类处置成 7 修 + 40 豁免。切片 C 要处理 T25 显式推下来的两件事：①判据 3 挡不住「**非裸但宽于理由**」的 pattern；②`scripts/**` 在**零个** workflow 的 `paths` 里（守卫可静默腐烂）。

**判据 4：把「pattern 被放宽」变成可观测。** T25 的残余风险是实测出来的，不是推的：消融 M4 把一条规则从 `new_module/impl.rs` 放宽成 `*`，两条既有判据**都不响**、`--audit` 照旧打印 `0 unclassified` —— 一份**看起来完全健康**的输出。切片 C 的解法不是再加一条形状规则（打地鼠），而是给每条 `MENTION_EXEMPT` 规则一个**声明的命中数**：元数 `(spec, pattern, reason)` → `(spec, pattern, expected_hits, reason)`，自检里对账，报 `declares N mention(s) but matches M`。**之所以可行，是量出来的**：规则侧命中 `{4,1,1,1,2,2} = 11`、`SUBJECT_REMOVED` 侧 `27+2 = 29`，**11+29 = 40 = 悬空总数** ⇒ 豁免是「**按条且穷尽**」的，所以数的变化必然可观测。**有意的不对称**：`SUBJECT_REMOVED` **不加**计数 —— 它的作用域按设计就是整个文件，「命中几个」在那里不是安全性质，防它静默的是判据 2（spec 正文必须自认删除提交）。两张表的元数不同是**自证的**，不是疏漏。实现落在判据 1 的 `elif` 分支上，这样一条**死规则**只报一次（死规则不该同时被抱怨计数不符），有专门测试钉住。

**同时修掉一处「已变成假话的注释」。** 同一个注释块写着 *"Two invariants … Both are checked"*，紧接着列了**三条** —— T25 加判据 3 时写歪的。改成四条并落本票两个裁决。（**过期规格比没有规格更危险**的同源问题：注释自带权威感，读到的人不会去数。）

**判据 3 与判据 4 互相独立 —— 消融实测，且本轮重跑复核**（不采信上一轮自己的结论）：M0 干净；M1 把 `new_module/impl.rs` 的 1 改成 3 → **只红判据 4**；M2 `tui/src/**` → `*`（该 spec 恰好只有 2 条悬空，**命中数不变**）→ **只红判据 3**；M3 `index.json` → `*.md`（**非裸**、但吞掉 `record-session.md` 的 2 条）→ **只红判据 4** ← 这正是判据 3 看不见的那个洞；M4 `record-session.md` 声明数改成 9 → 只红判据 4。**两次打红的集合不相交**（T19 判据：打红同一集合 ⇒ 其中一条是装饰）。**M5 端到端**：把守卫复制到 `scripts/_ablation_check.py`（同目录 ⇒ `ROOT = parents[1]` 仍正确）、改一处计数、跑它 —— 打印 `self-check found 1 problem(s)` 与 `hit count drifted`，而 **`exit` 仍是 0** ⇒ advisory 语义未被削弱。

**裁决 1：mentions 的悬空计数维持 advisory；被强制执行的是「表的一致性」与「仓库自证」。** 守卫 exit code **不变**（判据 1–4 全部 advisory）—— 这是它的设计性质：**a mention is not a reference**，让散文里提到一个文件名去红 CI 会造假红。强制的部分落在**测试**里：表一致性（`exemption_self_check(real) == []`）与 `unclassified == 0`；后者是**仓库自己的主张**，不是工具的性质，测试 docstring 写明了。代价是「新出现一条悬空提及会红，修法是加一行带理由的规则」—— 这是**有意的摩擦**。

**裁决 2：CI 接线取「无 `paths` 过滤」。** 理由是一手的：守卫用 `os.walk(ROOT)` 建索引 ⇒ **它读整个仓**，所以 `crates/**`/`python/**`/`packages/**` 的**增删**同样能翻判词（加文件可能让规则变死规则、删文件可能产生新悬空）⇒ `paths: scripts/**` 会**恰好对翻判词的那类改动瞎**。代价侧**由 CI 自己实测背书**：这个新 job 首次运行 **23s**，所以「每次 push 都跑」几乎免费（故刻意做得很小：不建 Rust、不跑整套 Python 套件）。副作用（有意）：`scripts/**` 与 `.trellis/spec/**` 的提交**从此会触发一个 job** —— T21–T25 那种「收口提交零触发」的记录，从 T26 起不再成立。

**🔴 第一次 CI 直接打红 —— 而它抓到的是真缺陷，不是我测试写错。** 推送 `6e748e8` 后 **Scripts CI 与 Python CI 同时红**（`35207013920` / `35207013916`）。失败点是新测试的 `test_real_corpus_has_no_untriaged_dangling_mention`，断言逐字为 `Left contains one more item: ('.trellis/spec/backend/agent-capability-spec.md', 380, 'config.toml')`。**根因**：守卫的 `_repo_index()` 用 `os.walk` —— **它看得见本机被 gitignore 的文件**。本机存在 `./.codex/config.toml`（被 `.gitignore:90` 忽略），而 CI 的干净检出里没有 `.codex/`。于是**同一个提交**：本机 `MENTION_RESOLVED`、CI `DANGLING` → 未分类 → 测试红。**这正是守卫自己在 `EXCLUDE_DIRS` 注释里为 `.scratch/` 写下的失效模式，只是当时只针对一个目录，没有上升到「类」。** 一手规模：`os.walk` 索引 **1320** 条路径 vs `git ls-files` **1154** ⇒ 本机独有约 **13%**，不是角落情况。

**修法换根因而非打地鼠：索引源从「文件系统」换成「git 跟踪集」。** 索引改为 `git ls-files` **∩** 保留 `EXCLUDE_DIRS`。**两者都必需**：① 只用 git ⇒ 会把 **12 个「在 `.scratch/` 被 ignore 之前就已提交」**的历史文件放回来（`.scratch/durable-runtime-migration/**`，一旦被跟踪 `.gitignore` 对它失效）⇒ 等于**撤销 T24 的排除决定**（当时 `.scratch/pt-test_*/**/lib.rs` 把 `lib.rs` 候选从 4 抬到 79）；② 只用 walk 就是本次事故。三索引实测：walk+EXCLUDE `488 basenames / 1320 paths`、纯 git `468 / 1154`、**git ∩ EXCLUDE（采用）`456 / 1142`**。**爆炸半径是量出来的不是假设的**：A → C 的判词差异 **1/325**（恰是那一条）；**C \ W = 0** ⇒ 换源只会**移除**索引项、不会新增；**W \ C = 178**，用 `git check-ignore --stdin` 逐条判定 **177 被 gitignore**（`.opencode` 55 / `.agents` 48 / `.reasonix` 47 / `.workbuddy` 10 / `.codex` 8 / `.kimi-code` 6 / `.trellis` 1 / `packages` 1 / `uc.repos.yaml` 1），余 1 条是本票当时尚未入库的 `task.json`。同时把 `config.toml` 按「**运行时存在、设计上不在仓内**」处置（Codex CLI 自己的 `$CODEX_HOME/config.toml`，spec 样例自己就用 `tempfile.mkstemp(..., dir=codex_home)`，与 `uc.scheduler.yaml` 同类；全 spec 目录里恰好 1 处）。

**数字（修后）**：`--audit` **exit 0**；`89 ok / 1 stale / 8 ambiguous / 0 structural` **一格未动**（与 T24/T25 收口逐项相同）；`mentions 41 of 227`（**41 exempt** = 规则侧 12 + 整篇 29）/ **0 unclassified**；悬空 **40 → 41**；Python 收集 **1149 → 1160**（+11）。**两次自我更正**：① 首轮消融我把 M3 的预期写错了（用 `new_module/impl.rs → *`，但那份 spec 恰好只 1 条悬空 ⇒ 裸通配符没改变命中数，突变**退化成 M2 同形**）⇒ 换成 `index.json → *.md` 这才是「非裸但更宽」的形状；② 早期 jsonl 两行（「10 条测试」「mentions: 40 of 227」）是修复**前**的实测，已**显式标注为过时**而非静默改写。由此也**修正 T25 的一处记账**：T25 的 `40 exempt` 只在**本机**成立（本机有那个被忽略的 `.codex/`），干净检出里当时就已经是 41 —— **不是算错，是环境相关的**，而本票正是要消掉这种东西。

**门禁**：`ruff check` 对守卫与新测试文件均 **All checks passed**；⚠️ `ruff check scripts/` 整体**仍是红的**（`I001`+`UP045` 都在 `scripts/check-codex-issue-flow.py`，`fc9b5ce` 起既有）⇒ 新 workflow 只**点名文件**；`ruff format --check` **不接**（改动前后都失败）；两文件 `ast.parse(feature_version=(3,9))` 通过。**CI（本票唯一的真跑验证）**：Scripts CI `35208696969` 全绿（`spec reference guard (Python 3.9)` 与 `(Python 3.12)` 各 success）；Python CI `35208696908` 全绿（ruff lint / dashboard checks / test 3.9 / test 3.12 全 success）；其 3.9 测试 job 自报 **`1152 passed, 8 skipped`** = 收集 **1160**，与本机 `--collect-only` **逐数一致**；11 条新测试在 CI 日志里**逐字 `test <name> ... PASSED`**，skipped 仍是基线的 8（**无新增 SKIP 行**）⇒ **真跑而非静默跳过**。

**一处新踩到的静默陷阱（已记账）**：用 `subprocess.run(..., text=True, input="\n".join(paths))` 喂 `git check-ignore --stdin` 时，`text=True` 会把 stdin 的 `\n` 翻成 `\r\n` ⇒ **每个路径多带一个 `\r`** ⇒ git 认为含控制字符便**加引号回显** ⇒ 集合比较 175/178 全不匹配，而 `rc=0`、行数看着正常。改传 **bytes** 后 `177 被忽略 + 1 未跟踪` 与总数 178 逐条对上。

**提交**：`6e748e8`（实现，6 files / +592 −14）+ `53550fd`（修索引源，4 files / +249 −8）+ `ce81644`（归档）。⇒ **#675 的切片 A / B / C 至此全部交付，可关票。**


### Git Commits

| Hash | Message |
|------|---------|
| `6e748e8` | (see git log) |
| `53550fd` | (see git log) |
| `ce81644` | (see git log) |

### Testing

- 消融（守卫侧，本轮**重跑复核**、不采信上一轮结论；`.scratch/t26-ablation.py`）：M0 干净 / M1 计数写错 → **只红判据 4** / M2 `tui/src/**`→`*`（该 spec 恰好 2 条悬空，命中数不变）→ **只红判据 3** / M3 `index.json`→`*.md`（**非裸**）→ **只红判据 4** ← 判据 3 看不见的那个洞 / M4 声明 9 实际 2 → 只红判据 4。**判据 3 与 4 打红的集合不相交**（T19 判据）。M5 端到端（`scripts/` 下临时副本）：打印 `self-check found 1 problem(s)` 而 **exit 仍 0**。守卫 sha `ddaadb29b048251a` 按字节复原校验通过。
- 消融（测试侧；`.scratch/t26-test-ablation.py`）：基线 **11 passed**；拆掉判据 4 → **恰好 2 条**红（`test_declared_count_must_match_the_corpus`、`test_pattern_wider_than_its_reason_is_reported`）；拆掉判据 3 → **恰好 1 条**红（`test_bare_wildcard_pattern_is_reported`）；两次突变后守卫 sha 均按字节复原。
- 索引复算（`.scratch/t26-index-remetric.py` + `-remetric2.py`）：W(walk) `1320 paths / 488 basenames`、G(纯 git) `1154 / 468`、C(git ∩ EXCLUDE，采用) `1142 / 456`；**A→C 判词差异 1/325**；**C \ W = 0**（换源只会移除索引项）；**W \ C = 178**，用 `git check-ignore --stdin` 逐条判：**177 被 gitignore** + 1 条本票尚未入库的 `task.json`（178 逐条对上）。
- 真实语料：`python scripts/check-spec-refs.py --audit` → **exit 0**、`89 ok / 1 stale / 8 ambiguous / 0 structural`、`mentions 41 of 227`（41 exempt / 0 unclassified）。
- 门禁：`ruff check` 对守卫与新测试文件 **All checks passed**；两文件 `ast.parse(feature_version=(3,9))` 通过；`pytest tests/python/test_check_spec_refs.py -o addopts=""` → **11 passed**；收集总数 **1149 → 1160**。
- **CI（本票唯一的真跑判据）**：Scripts CI `35208696969` **绿**（`spec reference guard (Python 3.9)` 与 `(Python 3.12)` 各 success）；Python CI `35208696908` **绿**（ruff lint / dashboard checks / test 3.9 / test 3.12 全 success）；其 3.9 测试 job 自报 **`1152 passed, 8 skipped`** = 收集 **1160**（与本机 `--collect-only` 逐数一致），11 条新测试在日志里**逐字 `PASSED`** 且 **skipped 仍为基线的 8**（无新增 SKIP 行）⇒ 真跑而非静默跳过。
- ⚠️ **第一次 CI 红的处置留痕**：`6e748e8` 两档**同时红**（`35207013920` / `35207013916`）⇒ 根因是 `os.walk` 索引（本机有 `.codex/config.toml`、干净检出没有 ⇒ 同一提交两个判词）⇒ `53550fd` 换索引源后两档全绿。**本地永远测不出这个红**。

### Status

[OK] **Completed**

### Next Steps

- **#675 的三个切片（A/B/C）至此全部交付 ⇒ 关票**（贴验收映射）。
- ⚠️ **两篇已删子系统的 spec 正文仍只有横幅**（`tui-grpc-spec.md` / `local-worker-bridge-spec.md`）：逐节重写需每节重新取一手证据，**应另开票**。
- **#674（journal 账本欠账）**：14 个 session 的 `### Testing` 段仍是脚本骨架、Session 13 有重复残块 —— 与本票无关，保持 open。本场按「填掉自己那两个占位」执行，但历史欠账需专门处理。
- ⚠️ **判据 4 的固有上限**：它钉的是**数量**不是**语义** —— 换成另一个同样只命中 1 条、但理由不成立的 pattern，对账看不出来；豁免理由是**散文**，不可机器校验。
- ⚠️ **索引换源的固有上限**：`git ls-files` 依赖 `.git` 存在；非 git 场景回退 `_walk_index()`（有 stderr 警告），此时谓词仍可能随本机文件而变。
- 副作用（有意）：`scripts/**` 与 `.trellis/spec/**` 的提交**从此会触发一个 job**，T21–T25 那种「收口提交零 CI 触发」的记录不再成立。


## Session 30: T27: #674 —— journal 账本整备：39 处占位符据实回填 + 5 处重复残块清除

**Date**: 2026-09-17
**Task**: T27: #674 —— journal 账本整备：39 处占位符据实回填 + 5 处重复残块清除
**Branch**: `main`

### Summary

清掉 journal 的存量欠账。先量形状再动手，实测出票面漏掉的两类（骨架尾巴残留 4 处；空标题重复 4 处 + `(Add details)` 6 处），把三者归因为同一个机制「在追加的骨架之上手写正文」。14 个 session 的 `### Testing` 据实回填（数字本就在同段的 Summary / `## Gates` 里，每条标注来源；Session 1 确无本地数字 ⇒ 写「当时未记录」）；19 个 `- None - task complete`（票面说 4 个）逐条换成可核验的后续；删掉 Session 13 的 590–601 stub 与 4 处占位尾巴。结果：三类占位符 0/0/0、26 个唯一 session、统一 CRLF、numstat 43/93。**故意不补** S16/17/18 的 `### Git Commits`（从散文里拼表等于猜哪些提交属于该 session）。

### Main Changes

## 普查（一手，先量形状再动手）

票面列了三类欠账，实测**多出两类**：

| # | 缺陷类 | 票面 | 实测 |
|---|---|---|---|
| 1 | `### Testing` 只剩骨架占位符 | 14 个（1–11 / 19 / 20 / 21） | **14 个，逐项吻合** |
| 2 | Session 13 重复残块 | 590–601 | **吻合**（被取代的 stub） |
| 3 | `- None - task complete` 待复核 | 「另有一批（12 / 14 / 15 / 22）」 | **19 个**（1–15 / 19–22）—— 票面**不全** |
| 4 | **骨架尾巴残留**（重复 `### Testing` + `### Status` 占位对） | 未提 | **4 处**（S12 / S13 / S14 / S15） |
| 5 | `### Main Changes` 仍是 `(Add details)` | 未提 | **6 个**（1 / 2 / 3 / 9 / 10 / 11） |
| 6 | 空标题重复（`### Main Changes` ×2、`### Summary` ×2） | 未提 | **4 个**（S12 / S14 / S15 / S20） |

关键分解：`18` 处 Testing 占位符 = **14 处骨架型 session + 4 处骨架尾巴**。
不做这步分解，就会把「删掉尾巴」误当成「回填 14 处」。

⚠️ **没用票面给的复算脚本**：它用 `re.split(r"^## Session .*$")` 切段再 `index("### Testing")`，
既会在重复标题上错位（T24 实测把 18 数成 19），又会在「整段没有 `### Testing`」时直接 `ValueError`
（Session 13 的 stub 正是这种）⇒ 一律用 `finditer` 的 **start() 位置**切片。

## 机制归因：是**同一个**事故，不是三次

脚本骨架被追加后，作者是**在骨架之上手写正文**的：正文自带 `### Summary` / `### Main Changes`
重新起头 ⇒ 同名标题出现两次；正文写完后，**骨架的尾巴**（`### Git Commits` + 占位 `### Testing`
+ 占位 `### Status` + `### Next Steps`）**原样留在下面** ⇒ 占位与真内容并存。
Session 13 是极端情形：**整个骨架块被写了一遍又被取代**。尾巴里的 `### Git Commits` 哈希表是**真的**，必须保留。

**删 stub 不丢信息**：stub 的 Summary 记 `Rust 237 / pytest 1106+5`，真块（666 / 669）里
**同样写着** 237 passed 与 1106 passed / 5 skipped ⇒ stub 是其**子集**。

## 回填：证据本来就在账本里

14 个 session 的门禁数字**早就在同一段**，只是写在 `### Summary` 或手写的
`## Gates` / `## 门禁终值` / `## 质量门禁` / `## 测试` / `## 门禁` 里 —— 每条回填都标注
来源（`源 = 本段 …`），**没有一个数字是推的**。

- **Session 1 是唯一没有本地数字的**（只有「PR #627 的 15 项 CI 检查全绿」）⇒ 据实写「当时未记录」，不补数。
- 6 个 `(Add details)` 的 session 正文确实只有 Summary ⇒ **不回填内容**，只给归档任务目录的指针。

## `- None - task complete`：19 条，判据两条

① 本段正文里**明写**的 `## Next` / `## 状态` 陈述（S5 / S6 / S12 / S13 / S15 / S22）；
② 否则用**下一个 session 的标题所指票据**（13 条）。
⚠️ 这句话本身是坏的 —— 它把「脚本没填」与「确实没有后续」呈现成**同一句话**，
所以**没有**用「无后续」去替换它。

## 手术与验收

单遍、可审计：`DEL` 表 **9 个区间共 54 行**（S13 stub 12 行 + 4 处占位尾巴各 8 行 + 4 处空标题），
**逐字节断言**后才删；`REP` 表 **39 行**（14 处 Testing + 6 处指针 + 19 处 Next Steps），
每行断言原内容是哪个占位符；并断言「删除集 ∩ 替换集 = ∅」。任一不符**不写盘**。

⚠️ **嵌入 `\n` 的替换值在写盘前转成 `\r\n`** —— 文件是 CRLF，直接写 `\n` 会留孤立 LF
（T24 在 SKILL.md 上真踩过，且**断言是在文件已被写坏之后**才响）；本票把这一步**前移**，
并加 `assert "\n" not in new_text.replace("\r\n","")` + 写后回读比对 + 行尾复核。

验收 7 条全过：三类占位符 **0/0/0**；每个 session 的 6 个标准标题**各恰好一次**；
`## Session N` **26 个唯一编号**（术前 Session 13 出现两次）；`### Testing` 最短 **116c** 全非占位；
**统一 CRLF、零孤立 LF**（1950 / 1950）；`numstat` **43 / 93**（受控改动，非整文件重写）；
既有门禁不受影响（守卫 `--audit` 仍 89 ok / 1 stale / 8 ambiguous / 0 structural、exit 0，
`test_check_spec_refs.py` **11 passed**）。


### Git Commits

| Hash | Message |
|------|---------|
| `afb2333` | (see git log) |
| `75c7d22` | (see git log) |

### Testing

- **本票的门禁就是「账本自身的结构不变量」**（不动代码 ⇒ 没跑任何套件）：
  - 三类占位符计数（脚本内实测）：`- [OK] (Add test results)` **18 → 0**、`(Add details)` **6 → 0**、
    脚本默认的 Next Steps 句 **19 → 0**；
  - 每个 session 的 6 个标准标题**各恰好一次**（26/26；唯一例外是 S16/17/18 本就缺 `### Git Commits`）；
  - `## Session N` **无重复**（26 个唯一编号；术前是 27 个标题）；
  - 行尾**统一 CRLF、零孤立 LF**（前后都用**字节计数**核，不用 `cat -A` 过管道 —— 那不可信）。
- **手术本身的可审计性**：9 个删除区间**逐字节断言**它要删的内容、39 个替换行断言原内容是哪个
  占位符、并断言「删除集 ∩ 替换集 = ∅」；任一不符**不写盘**；写盘后回读比对 + 行尾复核。
- **回归探针（既有门禁不受影响）**：`scripts/check-spec-refs.py --audit` → `89 ok / 1 stale(advisory) /
  8 ambiguous(advisory) / 0 structural`、**exit 0**、`mentions: 41 of 227 (41 exempt, 0 unclassified)`；
  `pytest tests/python/test_check_spec_refs.py -o addopts=""` → **11 passed**。
- ⚠️ **本票未跑**：任何 Rust / Python 全量套件与历史门禁（票面非目标，且当时环境不可复现）；
  `journal-1.md` **不在任何测试的覆盖范围内** —— 这正是它能悄悄长出 39 处占位符的原因。

### Status

[OK] **Completed**

### Next Steps

- **存量清了，产量还在**：`add_session.py` 的骨架**仍会**产出这三个占位符 ⇒ 从下一个 session 起
  还会再长出来。治本两条路（本票不做，已记账）：改脚本，或把「占位符计数 == 0」做成一条**收口检查**
  —— 建议并入 `ci-scripts.yml` 做成 advisory，与守卫同形（**别改既有 workflow 的 `paths`**）。
- **Session 16/17/18 缺 `### Git Commits`**：本票**故意不补** —— 17 的正文只出现过 `0c2604d`（CI 验收提交）、
  18 是 `dd3d2b3`，而 16 一个自己的哈希都没有 ⇒ 补表等于**猜「哪些提交属于该 session」**。
  建议另开票，用 `git log` 按日期/标题回溯。


## Session 31: T28: #676 —— journal 账本收口检查（占位符整行相等 == 0）+ 回填 S16/17/18 的 Git Commits

**Date**: 2026-09-17
**Task**: T28: #676 —— journal 账本收口检查（占位符整行相等 == 0）+ 回填 S16/17/18 的 Git Commits
**Branch**: `main`

### Summary

把「占位符不再长回来」做成收口检查（独立小 workflow，判据 = 整行相等），并按实测口径回填 S16/17/18 缺的 Git Commits；顺带量出语料其实是五份 journal / 两本账。

### Main Changes

## 普查：票面两处前提被推翻

先量形状，不照票面开工。

| 票面说法 | 一手实测 | 影响 |
|---|---|---|
| 「两份 journal」 | git 跟踪 **5 份 / 2 本账**：`Jamery Wang/` 30 session；`JameryW/` 3 份文件 123 session、**363 处占位符** | 直接对全语料收口 = 一条**永远红的检查**（正是本票自己警告的形状） |
| 三个 session 的日期是 2026-09-16 | 三段 `**Date**` 都是 **2026-09-15** | 按日期回溯会拉错窗口 ⇒ 改用 `git log -S` 定位「记录该 session 的提交」 |

两本账**都已入库**（`git ls-files '.trellis/workspace/**'` 9 个文件）⇒ 干净检出也能看见，判词一致。

## 判据：整行相等，不是子串

`journal-2.md` 就是记录 #674 的那份 —— 它的正文**本身在引用这三个占位符**：

| 判据 | `- [OK] (Add test results)` | `- None - task complete` | `(Add details)` |
|---|---|---|---|
| 宽松（子串） | 1 | 3 | 4 |
| **严格（整行相等）** | **0** | **0** | **0** |

⇒ 子串判据在**干净**账本上就报红；红着的门禁会被关掉，**那比没有门禁更糟**。
代价是一条文档规则（引用占位符时要加前缀），已写进守卫 docstring 与违规信息。

## 守卫：`scripts/check-journal-ledger.py`

- **FAIL（exit 1）**：`PLACEHOLDER`（整行相等）/ `HEADING_COUNT`（每 session 六个标准标题各恰好一次）/
  `SESSION_NUMBER`（必须带编号且不重复）/ `EMPTY_CORPUS` / `NO_INDEX` / `STALE_SKELETON` / `LEGACY_DRIFT`。
- **ADVISORY**：行尾不统一；`index.md` 的 `Total Sessions` 与实测不符。
- **检测必须宽于解析**：任何以 `## Session` 开头的行都算 session 起点 —— 否则 `## Session: …`
  这种掉了编号的标题不是「坏 session」而是**不存在**，整段连同占位符一起漏读。
- **`STALE_SKELETON` 是检查器看自己**：已声明的标题/占位符必须仍在 `add_session.py` 里 ——
  骨架改名后判据必须立刻失效，而不是静默空转。
- 刻意**不**门禁 `(No commits - planning session)` 与 `[OK] **Completed**`：同一个产出者、
  不同含义（规划期确实没有提交；`Completed` 是真实状态）—— 门禁它们只会再造一条永远红的检查。

## 索引源 = `git ls-files`，不是 `os.walk`

`.trellis/.gitignore` 第 2 行就是 `.developer` ⇒「当前开发者」是**本机状态**，干净检出里
`get_developer()` 返回 `None` ⇒「这本账」**在 CI 里不可计算**。而文件系统走查会看见未被跟踪的文件
⇒ 同一个提交在本机与干净检出给出两个判词（T25 的 `40 exempt` 就是这个性质，T26 为
`check-spec-refs.py` 修过）。⇒ 语料 = git 跟踪的 journal；索引问不出来时 `NO_INDEX` **fail closed**。

## 另一本账：计数并冻结，而不是无视

`JameryW/` 那本（最后活动 2026-08-06，在 T 系列之前）不在本票范围内 ⇒ 数字被 **pin** 住
（`165/56/56`、`168/57/56`、`30/10/10`），pin 对不上就 FAIL —— T26 判据 1「声明的计数必须与语料相符」。
**pin 只对表内路径生效**：pin 目录下新增的 journal 仍按「我们的」判 ⇒ 必须干净。
计数过的债是可见的，没计数的不是。

## 回填 S16/17/18 的 `### Git Commits`：口径是量出来的

把 journal-1.md 里**已有表格的 25 个 session** 逐个对照 `git log -S`（找出「记录该 session 的提交」）：
**25/25 都不把自己那条记录提交列进本表**，表内是**在该提交之前落地的交付提交** ——
机制解释：表写于记录 session 的那一刻，写不进还不存在的提交。

| session | 记录它的提交（按口径不列入） | 回填条目 |
|---|---|---|
| S16（T14 #659） | `f28bf24` | `ca67b20`（T14 实现，`Tracker: #659`） |
| S17（T14 收口 / #664 / D15） | `5ea38c9` | `0c2604d`（#664 修复，`Tracker: #664`） |
| S18（T15 #660） | `d942fa9` | `dd3d2b3`（T15 实现，`Tracker: #660`） |

每条附一行回溯来源，并**点名未列入的提交**（`437265d` / `45e60b7` / `3d876df`）⇒ 映射完整、无静默丢弃。
手术可审计：插入点由「会话切段 → 段内唯一的 `### Testing`」定位（**不用**全局匹配 —— 那行在文件里出现几十次）；
3 个哈希各 3 条断言（存在 / 是记录提交的祖先 / 提交信息片段相符）在**构建输出之前**跑完；
候选文本在**写盘之前**就通过整份文件的终检（26 个 session 全部六标题各一次、占位符 0）。

## 消融自检与一处意外

9 条非等价突变**全部打红**且集合两两不同（M1 判据→子串 打红 5 条；M8 抑制 HEADING_COUNT 打红 4 条；
其余各 1 条），1 条**等价突变**（`strip(" ")`）带理由记录为预期绿 —— 理由：`read_journal` **先**归一化 CRLF，
任何比较都见不到 `\r` ⇒ 单点改比较不可能影响 CRLF 行为；为此把 CRLF 测试的断言**下沉到读入器本身**。

⚠️ **意外（诚实记录）**：第一次消融跑完后，守卫里残留了 M8 的 `if False:`（`+2` 字节），
而消融脚本自己的「复原 + 断言」当时是**通过**的 —— 是**另起一次调用复算 sha256** 才发现的
（`6fc139243f16c2fd` → `376faa1601891bec`）。手工还原那一行后哈希精确回到 `6fc139243f16c2fd`，
证明残留只有那一行。**教训：复原脚本的自断言是自指检查，不是独立证据；消融后必须由另一个进程复算哈希。**

## CI 落法

独立小 workflow `.github/workflows/ci-journal.yml`（**不改任何既有 workflow 的 `paths`**），
Python 3.9 + 3.12：`ruff check` 两个文件 → 真语料跑守卫 → `pytest`。

**这里用 `paths` 过滤是对的**（与 `ci-scripts.yml` 刻意不过滤相反 —— 那个守卫走查全仓，过滤会瞎）：
本守卫的输入是**封闭集**，五项全列：`.trellis/workspace/**`（语料）、
`.trellis/scripts/add_session.py`（声明的标题/占位符就是照它校验的，**漏了会静默空转**）、
守卫本体、它的测试、自身。不跑 `ruff format --check`（它对本仓 Python 本来就红，本 workflow 不拥有那份债）。


### Git Commits

| Hash | Message |
|------|---------|
| `2773fcf` | (see git log) |
| `3c0a641` | (see git log) |

### Testing

- **本票的门禁就是「账本自身的结构不变量」**（产品代码一行未动 ⇒ 没跑任何产品套件，跑的是自己写的那两条）：
  - `python scripts/check-journal-ledger.py` → **exit 0**：`this ledger: 2 file(s), 30 session(s), 0 placeholder line(s),
    30/30 session(s) conforming`；`legacy (pinned, not fixed here): 3 file(s), 123 session(s), 363 placeholder line(s)`；
    另有一条 ADVISORY（`JameryW/index.md` 写 `Total Sessions: 119` 而语料有 123 个标题）。
  - `pytest tests/python/test_check_journal_ledger.py -o addopts=""` → **27 passed**；`ruff check` 两文件
    **All checks passed**；两文件 `ast.parse(feature_version=(3,9))` 通过（CI 矩阵含 3.9）。
  - **严格 / 宽松双口径在真语料上复算**（测试里独立算，不调守卫自己的函数）：严格 **0**、宽松 **> 0**
    ⇒ 判据不可放松。这一条是同字节上的绝对钉，不是「代码与它自己一致」。
  - **未跟踪文件不进索引**：往**真**工作树里丢一份带裸占位符的未跟踪 journal ⇒ 仍 exit 0 ——
    只有索引来自 `git ls-files` 才可能成立（T26 的回归钉）。
- **消融自检**（守卫 `19364B / sha256 6fc139243f16c2fd`）：9 条非等价突变**全部打红**、失败集合两两不同
  （M1 判据→子串打红 5 条、M8 抑制 HEADING_COUNT 打红 4 条、其余各 1 条）；1 条等价突变（`strip(" ")`）
  带理由记录为预期绿；每条按字节复原并校 sha256。
- **回填手术**：`git diff --numstat` = **24/0**（3 × 8 行纯新增，无删除）；`journal-1.md` 158409B / 1975 行 /
  CRLF 1974 / 孤立 LF 0，26 个 session 全部六标题各一次。本文件（`journal-2.md`）加本 session 后 501 行 /
  CRLF 500 / 孤立 LF 0。
- ⚠️ **本票未跑**：任何 Rust / Python 产品套件与历史门禁（票面非目标，且当时环境不可复现）。

### Status

[OK] **Completed**

### Next Steps

- **另一本账 `.trellis/workspace/JameryW/` 仍未清理**：363 处占位符 + 3 处重复 session 编号（49 / 74 / 98），
  已被 pin 住（不会变坏，也不会被忘记）；建议**另开票**处置，或明确接受它作为历史数据冻结。
- **#656（P2 地图）仍阻塞于外部「方案第 21 节」原文** —— 不臆造；P2-1 / P2-2 / P2-3 本体都等它。
- **门禁的代价要记住**：整行相等 ⇒ 以后**讨论**占位符的正文必须给它加前缀（列表符 / 反引号 / 表格竖线）。
- **接新守卫进 CI 就照 `.github/workflows/ci-journal.yml` 的形状做独立小 job**，别去动既有 workflow 的 `paths`；
  且 `paths` 必须覆盖**判据的来源**（本票是 `.trellis/scripts/add_session.py`）—— 漏了就会静默空转。
- 守卫目前**不**校验行尾统一（只作 ADVISORY，因为它随 `core.autocrlf` 在 CI 与本机给出不同读数）。


## Session 32: T29: #677 —— 给 T24 的两处守卫子句补测试级护栏 + 更正过期理由

**Date**: 2026-09-17
**Task**: T29: #677 —— 给 T24 的两处守卫子句补测试级护栏 + 更正过期理由
**Branch**: `main`

### Summary

对 T24 改过的两处守卫子句补做测试级消融（含对照突变）：bold 那条**未钉住**（子句生效但删掉不红），.scratch 那条的注释理由在 T26 换索引后已归零。交付 1 条合成确定性钉 + 注释更正。

### Main Changes

## 由来：T24 改了两处子句，却只有一处做过消融

T24（#673 切片 B）在 `scripts/check-spec-refs.py` 改了两处：**bold 锚收紧为 identifier-exact**、
**`EXCLUDE_DIRS` 加 `.scratch`**。它的消融（`research/notes.md` §E）只覆盖第 2 处，
而且判据是 **CLI 输出差异**（T0/T1/T2，三次 exit 全 0）⇒ **没有任何测试会因这两处被撤销而变红**。

按铁律「没有红过的检查器不是证据」，这两处子句的失效**都没有护栏**。本票把它从
「子句生效」（T24 已证）升级为「**子句被钉住**」（本票要证）。

## 三格测试级消融（含对照突变）

突变全部是**纯删除/替换**、由正则从**文件字节**定位（不写死整行字面量 —— T24 首版正是死在 CRLF 上）。
基线：守卫 `39904 B`、sha256 `ddaadb29b048251a`、`11 passed`、exit 0、`89 ok / 1 stale / 8 ambiguous`、mentions `41/227`。

| # | 突变 | 打红 | 守卫输出 | 判词 |
|---|---|---|---|---|
| MC | **对照**：关掉「裸通配符」不变量（已知被钉住） | **1** | 无变化 | 夹具**证明能红** |
| MB | 删 `if IDENT_RE.fullmatch(b.strip())`（−33 B） | **0** | **变**：`1 stale → 2 stale`、`89 ok → 88 ok` | 🔴 **未钉住** |
| MS | 删 `EXCLUDE_DIRS` 里的 `.scratch`（−12 B） | **0** | **逐字相同** | ⚠️ 判定面效应 **0** |

三者失败集合两两不同、对照与其余**不相交** ⇒ 无装饰性突变、无相互掩盖。每格按字节恢复并复校 sha256。

## MB：子句确实生效，但没人能发现它被删

删掉过滤后，`error-handling.md:307` 那行的**散文词** `delete` 重新成为符号锚。判定取
「**定义离引用行最近**的符号」—— `delete` 在目标文件里有定义且比真锚更近 ⇒ 落在引用范围外 ⇒ **假 STALE**。
与 T24 记录的 `STALE 27→26 / OK 113→114` 同向同量（现为 `1→2` / `89→88`）。

⇒ **子句在起作用，11 条测试却一条不红**：任何重构都能静默删掉它，语料多出 1 条假 STALE 而无人察觉。

## MS：`.scratch` 的效应在 T26 换索引之后就归零了

`EXCLUDE_DIRS` 在 git 路径（守卫第 302 行）上确实生效，`.scratch` 仍在滤掉 **12 个被跟踪的**
`.scratch/durable-runtime-migration/**` 文件。但那 12 个的 basename（`map.md`、`T1.md`…`T7.md`、
`D4-*`…`D7-*.md`）**与语料里任何提及都不撞** ⇒ 删掉后守卫输出**逐字相同**。

而注释里 T24 写的理由 —— *"Excluding it changes exactly one verdict in the whole corpus"* ——
是 **`os.walk` 索引时代**的测量（当时本机 `.scratch/` 里有回滚副本与测试脚手架树，
`.scratch/pt-test_*/**/lib.rs` 把 `lib.rs` 候选从 4 抬到 79）；**T26 隔天把索引换成 `git ls-files`**，
未跟踪的 scratch 在构造上就进不了索引。

⇒ **同一条子句、同一份代码，判词从「改变 1 条判定」变成「改变 0 条」——变的是索引源，不是子句。**
这与 T25 的 `40 exempt` 同型：**环境相关的读数不能当作子句的固有性质**。
按铁律推论 A（过期文档自带权威感），**同票更正**。

## 附带量清：`EXCLUDE_DIRS` 在 git 路径上 9/10 是死的

逐条量「该目录下**被跟踪且后缀在 `CODE_EXT`** 的文件数」：

| 条目 | 跟踪文件 | 其中代码文件 |
|---|---|---|
| `.git` / `target` / `node_modules` / `.venv` / `__pycache__` / `dist` / `build` / `.pytest_cache` | 0 | **0** |
| `vendor` | 1 | **0**（后缀不在 `CODE_EXT`） |
| **`.scratch`** | **12** | **12** |

⇒ 生产索引路径上 10 项里 **9 项无影响**，它们**只对 `_walk_index` 回退路径**有意义
（那里必需：去掉会让 `target/` 之类被整棵走查）。**不是缺陷**，但必须写明 ——
否则下一个人会以为这 10 项都在生效，并据此「清理」它们。

## 交付

1. **新增钉** `test_bold_prose_is_not_a_symbol_anchor`（合成、确定性）：目标文件第 40 行定义 `delete`；
   spec 行含一条 `crates/one/target.py:2` 引用 + 一个**在目标文件里未定义**的真锚 `GhostThing` + 一句散文 bold。
   收紧时无可解析符号（`best is None`）⇒ **OK**；松散时解析出 38 行外的 `delete` ⇒ **假 STALE**。
   断言两层：直接断言锚集合（`"delete" not in _symbols_on(line)`）+ 断言行为（`symbol is None` 且 `verdict == "OK"`）。
   **不钉真语料计数**（会随任何合法的语料编辑而碎）。
2. **更正注释**（第 94–110 行）：三条与现状相符的陈述。**注释改写不改变行为** ——
   改动前后守卫 stdout **逐字相同**；`git diff -U0` 的 **37 个改动行全以 `#` 开头**（非注释行 0）。

## 门禁

- 守卫终值**一格未动**：`89 ok / 1 stale / 8 ambiguous / 0 structural`、mentions `41/227`、`exit 0`。
- `pytest tests/python/test_check_spec_refs.py` ⇒ **12 passed**（新增 1 条）。
- **新钉自身的消融**：在**最终交付件**上重跑三格 ⇒ **MB 从「红 0」变为「红 1」，红的正是新钉**；
  MC 仍红 1、MS 仍红 0，集合不相交；收尾 sha256 回到 `e4361a664def8bc6`。
- `ruff check` 两文件 **All checks passed**；两文件 py39 可解析。

## 🔴 两条教训

1. **带沙箱升级的命令可能被重复执行。** 插入新钉的脚本跑了两次，整块测试**落了两份**
   （`def` 出现在 199 / 243 行）—— ruff 报 `F811 Redefinition`，而**pytest 仍是 `12 passed`**
   （同名函数后者遮蔽前者）⇒ **只有 ruff 看得见**。去重脚本以「恰好两份且逐字节相同」为前置断言，
   并加**绝对尺寸兜底**（`10915 + 2229 = 13144 B`）；它**幂等安全**：只剩一份时断言中止、**不写盘**
   （第二次执行正是被这样挡下的）。⇒ **副作用脚本必须幂等，或有歧义就拒绝写盘。**
2. **git 的 subject 是「第一个空行之前的全部内容」，不是第一行。** 首版提交信息我先写了正文，
   `git commit -F` 把整段当成 subject；已 `--amend` 修成「单行 subject + 空行 + 正文」。


### Git Commits

| Hash | Message |
|------|---------|
| `e99638d` | (see git log) |

### Testing

- `pytest tests/python/test_check_spec_refs.py -o addopts=""` ⇒ **12 passed in 1.54s**（新增 1 条钉）。
- **新钉自身的消融**（在**最终交付件**上重跑三格）：对照红 1、**MB 红 1（红的正是新钉）**、MS 红 0；
  三者失败集合两两不同、对照与其余不相交；守卫按字节复原、收尾 sha256 `e4361a664def8bc6`（由另一个进程复算）。
- 守卫 `exit 0`、`89 ok / 1 stale / 8 ambiguous / 0 structural`、mentions `41/227`（**一格未动**）。
- 注释改动**行为中性**：改动前后 stdout **逐字相同**；`git diff -U0` 的 37 个改动行全为注释、**非注释行 0**。
- `ruff check` 两文件 **All checks passed**；两文件 `ast.parse(feature_version=(3,9))` 通过。
- 行尾：`check-spec-refs.py` / `test_check_spec_refs.py` / journal-2.md 均纯 CRLF，孤立 LF 全 0。

### Status

[OK] **Completed**

### Next Steps

- ⚠️ **`.scratch` 是否应继续对索引隐形是一个决策**（本票保留现行为并记录两种理由）：保留 = 防将来
  `.scratch/**` 里的跟踪文件与 basename 撞车；移除 = 跟踪文件本就是仓的一部分，守卫本该看见（T26 的原则）。
- **`_walk_index` 回退路径没有专门的钉**：「回退路径与 git 路径给同一答案」这件事只被合成语料**间接**覆盖，
  且该路径只在「非 git 检出」或 `git ls-remote` 失败时走到 —— 建议另开票。
- **`EXCLUDE_DIRS` 的 9 项死条目**：只写明、不重构（它们对回退路径是必需的）。
- **#656（P2 地图）仍阻塞于外部「方案第 21 节」原文** —— 不臆造。
- **`.trellis/workspace/JameryW/` 那本账**（363 处占位符 / 3 处重复编号）仍只被 pin 住，建议另开票。


## Session 33: 架构升级收口审计（四面）—— P0/P1 已关、P2 本体待外部原文；补记 T20 与 §六 覆盖边界 + P1 交接项落点

**Date**: 2026-09-18
**Task**: 架构升级收口审计（四面）—— P0/P1 已关、P2 本体待外部原文；补记 T20 与 §六 覆盖边界 + P1 交接项落点
**Branch**: `main`

### Summary

按四面（地图/未归档目录/文档尾部/已关票交接项）审计「架构升级是否全部完成」：P0 #632 与 P1 #644 已关，P2 #656 开放但开放决策 0 / 待落地票 0（本体需外部「方案第 21 节」原文）；查出两条真缺口 —— #644 的三条「记入 P2 输入」无承接物（其中残余 1 已被同日提交与 CI step 推翻一半），以及 #678/#679 关闭但无 Trellis 目录、无 journal session。

### Main Changes

## 由来：用户要求「检查架构升级任务是否全部完成」，按仓内已录的四面审计法执行

架构升级**没有父实现票** —— 它由**三张 wayfinder 地图 + 逐票 Trellis 目录 + 评估件**承载。
故「是否全部完成」不是读一个状态字段能回答的，须按四面取一手证据，外加引用完整性一面。

## 四面结果

| 面 | 对象 | 结果 |
|---|---|---|
| ① | 三张地图 issue | **#632 P0 CLOSED**（2026-09-14）/ **#644 P1 CLOSED**（2026-09-15）/ **#656 P2 OPEN** |
| ② | 未归档 `.trellis/tasks/` 目录（真欠账源） | **干净** —— 该目录下只有 `archive/`，**零个未归档任务** |
| ③ | 文档进度尾部 | **已是最新**（见下「一处自查更正」） |
| ④ | **已关闭票**里的交接项 | 🔴 **找到两条真缺口**（见下） |
| ⑤ | 引用完整性 | 两个守卫 exit 0 |

开放 issue 总数 = **1**（只有 #656）；T1–T29 **全部 `completed` 并归档**，逐票有 issue 映射
（#637–#643 / #650–#655 / #657–#677）。

## P0 / P1 / P2 的逐层结论

- **P0 ✅ 完成并关闭**：T1–T7（#637–#643）逐票关闭归档，D4–D7 全闭环，P0 验收通过。其唯一遗留
  （三笔 `#[ignore]` PG e2e）已由 P1 清账。
- **P1 ✅ 完成并关闭**：T8–T12（#650–#654）逐票关闭归档，D8–D12 全闭环；P1 验收是**逐条**反查
  覆盖测试 **并确认该测试真的被某个 CI job 执行**（不是只存在文件）。
- **P2 ⛔ 地图仍开放，但开放决策 = 0、待落地票 = 0**：卡点**不在我方** —— P2-1 / P2-2 / P2-3 的
  **本体**（Optimizer 用三个比率*算什么*、review 策略、market scheduling）在仓内**无任何定义**
  （硬证据：`crates/`、`python/`、`packages/` 三处 `market` 零命中，亦无独立 scheduler 模块），
  需**外部「方案第 21 节」原文**。**不臆造。**
  ⇒ **本仓能做的部分已全部做完；架构升级的「未完成」= 一个外部输入缺口，不是欠账。**

## 🔴 缺口 1（face ④）：#644 的三条「记入 P2 输入」没有承接物

#644 的关闭评论有一段 **「残余风险（不阻塞验收，记入 P2 输入）」**，共三条。实测：**在 P2 地图
#656 正文里 0 命中**（`ALL_AGENTS` / `清单漂移` / `live NATS e2e` / `本地无等价` /
`storage-integration` 计数全 0），#656 **0 条评论**，且**没有任何 issue 承接** —— 那次「记入」
实际只落在了**那张已关闭的地图**上。这正是 face ④ 要抓的失效形态。

已补记进 #656（评论 `#issuecomment-5723071300`），并在逐条核对中**推翻其中一条**：

**残余 1 的一半已经过期**（一手反证）：#644 那条说「T8/T12 的验收是单测级，**没有** live e2e，
T12 曾整体静默失效过 ⇒ 值得在 P2 补一条 live NATS e2e」。而
`tests/python/test_nats_live_dispatch.py` 落在 `7f69e62`（`2026-09-15 16:01:51 +0800` = **08:01Z**），
**早于该评论（`2026-09-15T10:28:18Z`）2 小时 26 分**；`82ea112` 收尾。

- 该文件跑**真 `nats-server -js`**、走真 `NatsWorker` 绑定路径
  （`_ensure_subtask_transport` / `_bind_per_worker_consumer`），**不是**在 mock 上重实现；
  其 docstring 逐字记着 `10099` / `10100` 与「affinity placement silently degraded to pre-T12
  behaviour」—— **正是残余 1 举的那个理由本身**。
- 5 条用例：`test_shared_and_per_worker_consumers_coexist` /
  `test_two_workers_bind_distinct_per_worker_durables` /
  `test_targeted_publish_lands_on_the_targeted_worker_only` /
  `test_shared_publish_still_reaches_the_overflow_queue` /
  `test_legacy_worker_is_served_by_overflow_alone`。
- **在 CI 里真跑**：`.github/workflows/ci-python.yml:86-106` 专设 step「Live NATS dispatch tests」
  —— 下载 `nats-server v2.14.6`、以 `-js -sd /tmp/nats-js -p 4222 -m 8222` 起服、
  **轮询 `/jsz` 验 JetStream 真起来**（验不到即 step 红）、再
  `pytest tests/python/test_nats_live_dispatch.py --integration -v`。

⇒ 精确的剩余缺口**只剩网关侧一半**：**没有**一条「真 Rust 网关 + 真已注册 worker」的 live e2e，
让 `placement_target` / `dispatch_gate` 在真实花名册上**自己决策**并把消息真投出去（Rust 侧仍是
纯函数单测）。**这是本仓首要铁律的又一例：间接信号 / 记忆 ≠ 一手事实，而一手事实就在旁边**，
且这正是「过期规格比没有规格更危险」—— 自带权威感。

残余 2 **成立**（已复核）：`tests/python/test_sandbox_env_allowlist.py:51` 定义 `ALL_AGENTS`、
`:64` 派生 `SPAWNABLE_AGENTS`、`:198` 用它做参数表，**无任何机制**从适配器注册处推导该清单
（清单漂移）。残余 3 是环境事实：本机无原生 PG，provider 是 Docker Desktop，可用性**逐轮实测**
（T17 可用 / T18 不可用），不可跨轮沿用。

## 🔴 缺口 2（交付流程偏离）：#678 / #679 关闭但**没有 Trellis 目录、也没有 journal session**

- 两票均 CLOSED（#679 `14:51Z` / #678 `15:51Z`），提交用 `Tracker: #679` / `Tracker: #678`。
- **全仓找不到它们的任务目录**：`.trellis/tasks/archive/2026-09/` 只有 37 个目录，最后一张是
  `09-17-t29-t24-clause-pins`；`find .trellis -name '*t30*' -o -name '*t31*'` **零输出**。
- **journal 账本也零命中**：`git grep -n -i 't30\|t31' -- .trellis` 为空；
  `grep -rn '#678\|#679' .trellis/workspace/` 为空；八个相关提交哈希在 journal 中全部零命中。
- journal 最后一笔是 **Session 32 = T29**；其后的 `87f1ae1` / `edcc861` / `a8f55e7` / `1624515` /
  `0033c74` / `7ba7ad8` / `f3ee6dd` / `ab07390` 八个提交（含**两票关闭**）**均无 session**。
- ⚠️ **顺带更正我自己的长期记忆**：记忆里把它们写作「**T30**」「**T31**」—— **这两个编号在仓内
  不存在**，是我自造的编号（T 线止于 T29）。这本身就是「把间接信号当事实」的同类错误。
- 判断：#678 是**整票规模**的交付（writer 修复 + 5 测试 + CI 接线 + 217 文件 / 263 引用迁移），
  却走了「issue → 实现 → 直落 main → 关票」而**跳过** 任务目录 / 归档 / journal 三步。
  **这不是已决冻结，是真偏离**，故记在此处而非默默略过。

## 一处自查更正（face ③）

本次审计开始时我按「§六 尾部止于 T19」推断「T20–T31 未在评估件记录」—— **前提是错的**。
实测 `task.json` 的 `map` 字段：**T16–T19 才是挂在本程序地图上（`map = 656`）的票**，
T20–T29 的 `map` 全是 `None`；而 T21–T29 的内容是 spec 引用 / journal 账本卫生
（#672 / #673 / #674 / #675 / #676 / #677）—— **属另一条线**。故评估件「不记 T21–T29」是
**正确的范围**，不是遗漏。真正该补的只有 **T20**：它是 T19 那处规范发现的直接续作，同改
`agent-capability-spec.md`（六个幽灵签名全仓命中 0、漏 `decompose`、base seed 与 advertised
set 混淆）。

⇒ 已把 T20 补进 §六，并**显式写出覆盖边界**（`54a5003`）：边界写出来，下一个审计者才不必重新
推一遍，也不会按「T 编号连续」把 T21–T29 误读成未交付的迁移票。

## 交付

1. `54a5003` —— 评估件 §六 补记 T20 + 显式写出覆盖边界（**+4 / −0**，纯 CRLF，179 → 183 行）。
2. #656 评论 `#issuecomment-5723071300` —— P1 三条交接项逐条落点 + 残余 1 的一手更正。
3. 本 session。


### Git Commits

| Hash | Message |
|------|---------|
| `54a5003` | (see git log) |

### Testing

- `python scripts/check-spec-refs.py` ⇒ exit **0**：`89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural`、
  `0 of 98 have no matching quoted content`、mentions `41 of 227`（41 全部带理由豁免、0 unclassified）。
- `python scripts/check-journal-ledger.py` ⇒ exit **0**：本账 `33 session(s)`、`0 placeholder line(s)`、
  `33/33 session(s) conforming`；legacy 3 文件仍按 `LEGACY_JOURNALS` pin（363 占位符）**未动**。
- 评估件改动按**结构性**量核验：`git diff --numstat` = **`4 0`**（纯增 4 行、0 删）；
  纯 CRLF `179 → 183` 行、bare LF **0**；写后回读与写前 sha256 对照
  （`d34dfb2a84a1c625…` → `0c921825693d9866…`），且落盘前对**全部**前置断言
  （字节数 48429 / CRLF 行数 179 / 两段目标文本均不得已存在）逐条通过才写。
- 断言的一手性：写进 #656 的两条核对（`ALL_AGENTS` 仍是测试内维护清单；投递面已有 live e2e 并在 CI 真跑）
  都是**直接读码 / 读 `git log`** 得到的，**不是**沿用 #644 的措辞。
- ⚠️ **未跑 cargo / pytest，理由是本票零代码变更**（唯一改动 = 评估件一段 docs + 一条 issue 评论），
  按仓规不触发构建门禁；CI 侧仍会走 `ci-scripts.yml`（**无 `paths`** ⇒ 任何 push 都跑）。
  这是**声明**而非「已通过」—— 若 CI 有红，本 session 的结论不变，但会在此追加。

### Status

[OK] **Completed**

### Next Steps

- **#656（P2 地图）本体仍是唯一阻塞点**：需外部「方案第 21 节」原文 —— **不臆造**。
- **P2 开票时必须纳入的两条输入**（本次已补进 #656，否则会随 #644 的关闭一起消失）：
  ① **网关侧** live e2e（真 Rust 网关 + 真已注册 worker 自己决策 placement / scope）——
  投递面那一半已由 `tests/python/test_nats_live_dispatch.py` 覆盖且 CI 真跑，别再当缺口；
  ② T11 的 `ALL_AGENTS` 清单漂移（新增 adapter 不会被自动纳入）。
- ⚠️ **#678 / #679 仍缺交付记录**：两票已关闭但**无 Trellis 目录、无 journal session**。本 session 只做
  **如实记录**，**没有代它们补写** —— 从提交信息反推 session 正文（尤其 Testing / Next Steps）会变成猜。
  若要补，建议按「补记一行」或另开一张记录票处理，而不是伪造一个 session。
- ⚠️ **Session 29 的 Next Steps 仍未被任何票承接**：`.scratch` 是否应对索引隐形是一个决策；
  `_walk_index` 回退路径没有专门的钉 ⇒ 建议**另开票**。
- **`.trellis/workspace/JameryW/` 那本账**（363 处占位符 / 3 处重复编号）仍只被 pin 住 ⇒ 建议**另开票**。
- ⚠️ **编号口径**：本次发现我自己的记忆把 #678 / #679 写作「T30 / T31」，而**仓内不存在这两个编号**
  （T 线止于 T29）。将来续排号一律以 `.trellis/tasks/*/task.json` 与 issue 为准，**不要沿用记忆**。


## Session 34: 补记 #679 + #678 的交付会话 —— 四面审计缺口 ② 的落地（编号连续 ≠ 同一条线：那两票无 T 编号）

**Date**: 2026-09-18
**Task**: 补记 #679 + #678 的交付会话 —— 四面审计缺口 ② 的落地（编号连续 ≠ 同一条线：那两票无 T 编号）
**Branch**: `main`

### Summary

四面审计（session 33）缺口 ② 的落地：`0033c74`(#679) 与 `7ba7ad8`+`f3ee6dd`(#678) 落地时未写 journal session，journal-2 止于 Session 32。本 session 按**一手产物**（提交正文 + issue 关闭评论）逐条转录，**不推测未留痕的过程**；两票均附 CI 结论与显式残余。

### Main Changes

本 session 为**补记**。交接事实（一手，非推断）：`0033c74` 与 `7ba7ad8`/`f3ee6dd` 三票落地时**没有写 journal session** —— journal-2 止于 Session 32（T27/T28/T29 那条框架线），其后 8 个提交（合入两票的关闭动作）都无 session 记录。此处按**提交正文 + issue 关闭评论**（两者都是一手产物，且都已推送）逐条转录，**不推测未留痕的心理过程**；本 session 的 `Testing` 段全部来自 CI 与提交正文的实测值，未重跑。

### 由来：为什么必须补

四面审计（Session 33）的第 ④ 面「已关票内结转项」发现两票缺交付记录。#679 与 #678 的关闭评论本身**证据完备**（各有验收映射表、提交表、CI 结论、显式残余）—— 缺的不是「交付记录」，而是**项目自己的 memory 账本里的那一行**。判据：journal 是 `add_session.py` 写、被 `check-journal-ledger.py` 守的唯一会话台账；一个提交有 issue 评论而无 session，就意味着**换一个 session 的我**读账本时看不到它存在。补写的可行性依据：内容可由已推送的一手产物**逐条还原**，因此是**转录而非猜测**。

### 票 1：#679 —— `task.py finish` 报告值与删除目标不同源

**现象（提交正文实测，修复前）**：

```
$ python .trellis/scripts/task.py finish
✓ Cleared current task (was: .trellis/tasks/09-13-t3-graph-runtime)
Source: session-fallback:qoder_64c9007e-...
$ python .trellis/scripts/task.py current
.trellis/tasks/09-13-t3-graph-runtime        # 一字未变，mtime 也未变
```

**根因（读代码）**：`clear_active_task` **删**的是 `_context_path(context_key)`（本 session 的文件），而**报**的值来自 `resolve_active_task` —— 后者在本 session 无文件时**回落扫描** `.runtime/sessions/*.json`（`Source: session-fallback:*` 即此）。⇒ **报告值与删除目标不同源**，删了 0 个却打印 `✓`。

> **严重性升级的一手事实**（票面未写、关闭评论补上）：`.claude/hooks/session-start.py:323` 在 **STALE POINTER** 分支里正是让用户 `Run python3 ./.trellis/scripts/task.py finish to clear the stale pointer` ⇒ **该状态的官方修法原本是静默空转**。

**改动（2 处代码 + 1 规格 + 1 测试 + 1 门禁）**：

| 文件 | 改动 | numstat |
|---|---|---|
| `.trellis/scripts/common/active_task.py` | `clear_active_task` 对 fallback 来源走**同文件里早已存在**的 `clear_task_from_sessions`（`cmd_archive` 早就在用） | +14/-1 |
| `.trellis/scripts/task.py` | `cmd_finish` **重解析后**再宣称成功；仍能解析出活动任务则退非零、不打印 `✓` | +17/-0 |
| `.trellis/workflow.md` | 更正「`task.py finish` deletes the current session file」这句**已变成假话**的规格 | +1/-1 |
| `tests/python/test_task_finish_fallback.py`（新） | 合成仓 + 子进程跑真 CLI，3 条测试 | +121 |
| `.github/workflows/ci-trellis.yml`（新） | **第一个覆盖 `.trellis/scripts/**` 的门禁** | +66 |

**关键设计点**：fallback 分支**只在恰好一个 session 文件存在时**触发 ⇒ 不可能删掉第二个窗口还需要的那根指针（两窗口测试断言的正是这一点）。

**验收映射（关闭评论，逐条）**：

| 票面验收 | 结果 |
|---|---|
| `finish` 后 `current` 不得再报出同一任务 | ✅ `test_finish_clears_a_fallback_sourced_pointer` 断言 `current` rc=1 且 stdout 为空 |
| 值来自 session-fallback 时必须清掉**那个** session 文件 | ✅ 同测断言该文件 `not exists()` |
| 回归：造「指向不存在任务」的 session 文件 ⇒ `finish` 后文件消失且 `current` 为空 | ✅ `TASK_REF` 即 `09-13-t3-graph-runtime`（**悬空**指针，与实测现场同形） |
| 不得把「没清任何东西」打印成 `✓ Cleared` | ✅ `test_finish_refuses_to_cross_delete_a_second_window` 断言 `"✓" not in stdout` 且 rc≠0 |

**顺序：先见红 → 再修 → 再消融。** 未修代码上 `2 failed, 1 passed`（repro 报 *the pointer that was reported is still on disk*；两窗口那条报假 `✓`；单窗口对照两态都过）；修后 `3 passed`；**M1**（删掉 fallback 分支）⇒ **只**打红 repro；**M2**（把重解析改成 `still = None`）⇒ **只**打红两窗口那条 ⇒ **两红集不相交**，两半都不是装饰。**恢复由独立进程复算 sha256** 与清单比对（连开两个独立进程），不让做突变的进程自证。

**两个附带结论**：

- 🔑 **`paths` 该不该加，判据是「输入集是否封闭」。** 新门禁**带** `paths`，而 `ci-scripts.yml` 刻意**无** `paths` —— 不矛盾：那个守卫扫**全仓**（任何文件都能翻判词 ⇒ 过滤会蒙住它），而本测试输入集**封闭**（`tmp_path` 自建语料 + 子进程 CLI + 不 import 本仓任何模块）⇒ 过滤才正确。**照抄「无 paths」是想错方向。**
- ⚠️ **顺带更正一个长期错误的基线**：Python 收集不是 `1160` 而是 **1188**（`6080a69` 的 CI run `35220680603` 自报 `1180 passed + 8 skipped`，与本机 `--collect-only` 逐数一致）。28 条漂移已完整归因：`2773fcf`（T28）新增 `test_check_journal_ledger.py` = 27 条，加 `test_check_spec_refs.py` 由 11→12 条（+1），**27+1=28** ✓。本票再 +3 ⇒ **1191**，CI 实测 `1183 passed + 8 skipped` ✓。

**未做 / 边界（有意不扩大）**：`.trellis/.template-hashes.json` **故意不动** —— 它记录**模板原样**；改写哈希等于宣称这个修复来自模板，会让未来的模板更新**静默覆盖**它。相邻缺陷（无 session 身份时 `finish` 直接打印 `No current task set` 而不走 fallback ⇒ 不假成功但也不清指针）**不在本票范围**。

### 票 2：#678 —— 归档不移写路径 ⇒ 787 条中 215 条悬空

**一手测量**：`.trellis/tasks/**` 下被 git 跟踪的 `*.jsonl`，逐行 `json.loads` 后取 `{"file": ".trellis/..."}`，用 `Path.is_file()` 判可解析性 ⇒ 引用总数 **787**、悬空 **217 → 215**（本票顺手修掉 2 条）、比例 **27.6%**。

**形态：全部同一个形状** —— 悬空目标**无一例外**是「归档前的旧路径」（`.trellis/tasks/<name>/…`，而目录早在 `archive/<YYYY-MM>/<name>/…`）。

**根因**：`task_utils.py` 的 `archive_task_dir()` 用 `shutil.move()` **只移动目录**，不触碰任务自己的 `implement.jsonl` / `check.jsonl`。这两个文件里的 `{"file": "<本任务的 prd/research 路径>", "reason": …}` 是**按归档前的相对路径**写下的 ⇒ **归档那一刻起就是悬空的**。**写出者制造了 100% 的实例 ⇒ 修在写出者而非周期性扫描器。**

**为什么一直没人发现（可复用的部分）**：`check-spec-refs.py` 只把 `.trellis/spec/**` 当引用语料 —— `.trellis/tasks/**` 的 `{"file": …}` **不在任何守卫的索引里**。实测佐证：本次改动（移动目录、改 4 条引用、删改若干行）之后，守卫输出**逐项未变**（`89 ok / 1 stale / 8 ambiguous / 0 structural`、`mentions 41 of 227`、`exit 0`）⇒ 与 **#675** 同型、不同对象：**引用完整性只在一处被守**。

**范围裁决（按行定，不是假设的）**：

| 类别 | 裁决 |
|---|---|
| 任务**自己**的引用，在 JSON 载体（`.jsonl`/`.json`） | **重指** —— 那里的 `.trellis/...` 字符串是机器写入的引用 |
| 任务自己在 `.md` 散文里的提及 | **不动** —— 散文里的路径可能是**故意的历史注记**（「本任务从 X 迁出」），且本仓对另一守卫的既有规则就是「a mention is not a reference」。**边界由测试断言**，不留隐含 |
| **其它**任务的引用 | **只在 stderr 报告，绝不改写** —— 归档的副作用去改别的任务的来源元数据，宽于命令本身 |

**同时抓到一处潜伏 bug**：本仓 **16 对**任务名互为前缀（`06-15-tui` < `06-15-tui-unit-tests`、`06-29-worker` < `06-29-worker-omp` …）。朴素 `str.replace(".trellis/tasks/06-15-tui", …)` 会**连带改写另一个任务**（可能归档在另一个月）的引用，**而结果仍像一个合法路径**。本语料恰好不命中（217 个迁移文件上 naive == boundary-aware），但框架会持续归档 ⇒ 匹配器改为**边界感知**。`find_external_task_refs` 有同一缺陷（并为该形状**产出假阳性告警**），一并修。

**改动与证据**：

| 提交 | 内容 |
|---|---|
| `7ba7ad8` | 框架修复 + 5 条测试 + CI 接线 + 规格更正（4 files / +377 −7） |
| `f3ee6dd` | 语料迁移：**115 个任务目录、217 个文件里的 263 条**引用 |

- 「语料未遗漏」：JSON 载体里、任务现居 `archive/` 的**归档前位置**引用 **263 → 0**（同仪器，HEAD vs 工作树）。
- 「迁移没做别的事」：217 个文件每个都与「前像施加前缀替换」**逐字节相同**（212/217 对生 blob；另 5 个只差 `git archive` 对 LF 存盘文件的 CRLF 转换）；**263 added / 263 deleted** = 每条引用一行；无行尾抖动。
- 「幂等」：对真实语料第二次 dry run 报 **0** 处改动。
- 「无新增 lint 债」：`task_utils.py` 的 ruff 在改动前后同为 6 条既有发现（3 UP024 / 2 E501 / 1 I001）。
- 「规格不再说谎」：`workflow.md` 把 `archive` 描述成「只清 session 指针」，在使该描述失效的**同一个提交**里更正。
- 测试：5 条，合成仓 + 真 CLI 子进程 + `--no-commit`，真树从不被改；**一处突变一次运行，各打红一个互不相交的非空集；边界突变恰好只打红它自己那条测试** ⇒ 无守卫是装饰。恢复由**独立进程**按 sha256 对清单校验。

**有意留开**：`.md` 散文里 2 条悬空的自我提及（可能是历史注记 ⇒ 需人裁，不做自动证伪）；JSON 载体里 7 条**跨任务**悬空引用（`06-27-optimize-…` ×6、`06-24-orchestrator-omp-phase3-python` ×1 —— 属其它任务，超出「一次归档的副作用」范围）。

### CI 接线

`ci-trellis.yml` 现在覆盖 `.trellis/scripts/**` 与两个测试文件，其 `paths` 选择**刻意与 `ci-scripts.yml` 相反**，文件头写明了理由（同上一节 🔑 的判据）。CI on `f3ee6dd`：**Trellis CI ✅（3.9 与 3.12 逐步绿）、Scripts CI ✅、Python CI ✅**；只有这三个触发，Rust/TS 按 `paths` 正确跳过。CI on `0033c74`：三门全绿。


### Git Commits

| Hash | Message |
|------|---------|
| `0033c74` | (see git log) |
| `f3ee6dd` | (see git log) |
| `7ba7ad8` | (see git log) |

### Testing

- **本 session 是补记，未重跑任何门禁** —— 下列数值全部转录自**已推送的一手产物**（`0033c74` / `7ba7ad8` / `f3ee6dd` 的提交正文与两个 issue 的关闭评论），不是本 session 的实测。**声明，而非「已通过」。**
- 转录后复核（本 session 唯一的新实测）：journal-2 = 84231 bytes / 937 CRLF / 0 孤立 LF；`check-journal-ledger.py` 报 `2 file(s), 34 session(s), 0 placeholder line(s), 34/34 conforming`。
- #679：修前 `2 failed, 1 passed`（repro 报 *the pointer that was reported is still on disk*）⇒ 修后 `3 passed`；**M1/M2 两红集不相交**；恢复由**独立进程**复算 sha256。CI on `0033c74`：**Trellis CI / Scripts CI / Python CI 三门全绿**（Python CI 两档 `1183 passed, 8 skipped`）。
- #678：5 条测试（合成仓 + 真 CLI 子进程 + `--no-commit`）；语料迁移 **263 added / 263 deleted**、**263 → 0** 悬空、第二次 dry run **0** 处改动、ruff 前后同为 6 条既有发现。CI on `f3ee6dd`：**Trellis CI ✅（3.9/3.12 逐步绿）、Scripts CI ✅、Python CI ✅**，Rust/TS 按 `paths` 正确未触发。
- ⚠️ **两票的 `Testing` 原文以 issue 关闭评论为准**（本文件是转录层）；本 session 对它们**没有**新增验证 ⇒ 若日后发现评论与提交不符，以**提交与 CI 日志**为准。

### Status

[OK] **Completed**

### Next Steps

- ⚠️ **两条未落地的相邻缺陷**（各自在关闭评论里显式记为「不在范围」）：① #679 —— 无 session 身份时（`resolve_context_key()` 返回 None）`finish` 直接打印 `No current task set` 而**不**走 fallback ⇒ 不假成功，但也不清那根指针；② #678 —— `.md` 散文 2 条自我提及 + 7 条跨任务 JSON 引用仍悬空（前者需人裁是否属历史注记）。
- **#656（P2 地图）仍阻塞于外部「方案第 21 节」原文** —— 不臆造；P2-1/P2-2/P2-3 本体都等它。
- ⚠️ **`add_session.py` 没有 dry-run**：`--no-commit` 只跳过 **git 提交**，**不**跳过写盘（本 session 亲测：探针调用直接写出一个 session 34，随后 `git checkout --` 逐字节回滚，72662 bytes / 812 行确认复原）。⇒ 想预览就**别碰真脚本**，或先 `git stash`/记录哈希。
- **补记类 session 的判据**（可复用）：只有当内容能由**已推送的一手产物逐条还原**时才补 —— 否则就是把「没记录」换成「编的记录」。本 session 的取舍：转录提交正文与关闭评论（都是一手且已推送），**不推测任何未留痕的过程**。
- **`.trellis/workspace/JameryW/` 那本账**（363 处占位符 / 3 处重复编号 49·74·98）仍**只被 pin 住**（`[legacy]`，`ADVISORY` 另报 index 写 119 而实为 123）⇒ 已由 `LEGACY_JOURNALS` 显式冻结并写明理由；**建议另开票**处置或明确接受它作为历史数据冻结。


## Session 35: T30: #680 —— 给 `.trellis/tasks` 的 jsonl 引用建守卫（#678 选项 C 的落地）

**Date**: 2026-09-18
**Task**: T30 / #680 —— `.trellis/tasks/**/*.jsonl` 的 `.trellis` 引用必须可解析
**Branch**: `main`

### Summary

交付 **#680 / T30**：把「`.trellis/tasks/**/*.jsonl` 的 `.trellis` 引用必须可解析」做成检查器 —— 即 **#678 自己建议的 `A + C` 里那个从未落地的 C**。新增 `scripts/check-tasks-refs.py`（守卫）+ `scripts/check-tasks-refs-selftest.py`（消融自检，同时是 CI 的一步）+ `tests/python/test_check_tasks_refs.py`（14 用例），并把独立小 job 接进 `ci-scripts.yml`。

真实语料实测：**787 refs / 14 dangling / 47 malformed**。前两项与 #678 一致（悬空数**修正了 #678 记录的 7**），第三项是**本票首次发现的另一个缺陷类**，已记账、**不在本票修**。

本 session 的**主要收获不是守卫本身，而是守卫在实现期暴露的三处「自己的错」**——其中两处会让守卫**看起来对、其实错**。逐条留在下面。

### 为什么 C 从未落地（一手证据）

```
$ grep -c 'jsonl' scripts/check-spec-refs.py      -> 0
$ grep -c '\.trellis/tasks' scripts/check-spec-refs.py -> 0
$ grep -n 'SPEC_DIR =' scripts/check-spec-refs.py
797:SPEC_DIR = ROOT / ".trellis" / "spec"
```

`check-spec-refs.py` 的语料是 **`SPEC_DIR = .trellis/spec`**，全文 `jsonl` 与 `.trellis/tasks` **零命中** ⇒ 任务上下文引用**不在任何守卫的索引里**。#678 修了**因**（`archive_task_dir` 归档时重指自引用，`7ba7ad8`；存量迁移 `f3ee6dd`），并写下建议 **"A + C"** —— **A 交付了，C 没有**。

### 三处「自己的错」（按发现顺序）

#### ① 守卫**无法对合成仓测试** ⇒ 第一版测试其实在审真仓

首版 `ROOT = pathlib.Path(__file__).resolve().parents[1]`。测试在 `tmp_path` 里造仓、`subprocess` 跑守卫 —— 但守卫**按自己的 `__file__` 定位真仓**。于是 13 个「合成仓」测试全部在审**真仓**，第一批之所以全绿是**数字碰对了**（真仓恰好 787/14）。

**判据**：一个测试在**换掉被测输入**后仍然通过，它就什么都没测。**修法**：加 `--root DIR`（测试传合成仓；CI 裸跑，保证判词只关于该提交），并对不存在的 git 仓退 2。

#### ② 索引来自 git、**语料来自文件系统** ⇒ 同一提交两个判词（T26 缺陷搬家）

首版 `collect()` 用 `TASKS_DIR.rglob("*.jsonl")` 取语料 —— **文件系统**。而未跟踪的 `.jsonl`（草稿）在作者机上被审、在 CI 上不存在 ⇒ **同一提交两个判词**。这正是本仓 T26 记为「`os.walk` 会见 gitignore 文件」的同一缺陷，只是从**索引**搬到了**语料**。

**实测（语料改跟踪集前后）**：

| 语料来源 | 工作树（本票目录在） | 干净检出 `worktree`（目录不在） |
|---|---|---|
| 文件系统 `rglob` | **788** / 14 / 47 | **787** / 14 / 47 ← 两个数不同 |
| 跟踪集（修后） | **787** / 14 / 47 | **787** / 14 / 47 ← 一致 |

**修法**：语料 = 跟踪集，并以 `test_untracked_carrier_is_not_audited` 钉住（突变 `corpus-from-filesystem` 打红它）。

#### ③ **基线的 788 是我数错了量**

我一度写下「788，因为本票自己的 jsonl 被计入」。**实测更正**：本票 `implement.jsonl` 里确有 9 处 `.trellis` 字面量，但**全部在 `detail` 散文里**；守卫**只统计 `file` 键**，本票只有 **1** 条（`.trellis/workflow.md`）。且它**提交前未被跟踪** ⇒ 守卫读 **787**；`git add` 之后读 **788**。**两者都合法**，测试断言 `in (787, 788)`。

**教训**：数「引用」必须用**守卫自己的口径**。我用 `grep` 数全部 `.trellis` token 得 9 —— 那是在数另一个量。

### 消融自检（入库为 CI 的一步）

`scripts/check-tasks-refs-selftest.py`：**6 个突变，全部变红，红集两两不同**，按字节恢复且由**独立进程**复算 sha256 认证（`a74deb2a…`）。

| 突变 | 打红用例数 |
|---|---|
| `existence-from-filesystem`（存在性改看文件系统） | 1 |
| `swallow-malformed`（吞掉坏行） | 3 |
| `never-dangling`（永不悬空） | 7 |
| `no-prefix-filter`（去掉 `.trellis` 前缀过滤） | 3 |
| `empty-exclude-set`（清空排除集） | 1 |
| `corpus-from-filesystem`（语料改回文件系统） | 1 |

**自检在本次抓到了两样东西，都删掉了**：

1. **一个装饰测试**：`test_exclude_set_is_applied_on_top_of_git` 初版把 `.scratch/note.md` 当「守卫的引用」来测排除集 —— 但该路径**不以 `.trellis` 开头**，会被前缀过滤**先**跳过 ⇒ **永远不会红**。改为直接断言 `_repo_index()` 的两半。
2. **一个假突变**：`crlf-sensitive-reader` 我试了两次 —— ① 改尾随 CR 分支只让守卫**崩溃**（崩溃会打红测试但**不钉住任何东西**）；② `split("\n")` 取代 `splitlines()` 是**零效果**（`line.strip()` 已去掉 CR）。**⇒ 连对应的 CRLF 测试一并删除。**

**判据**：一个**永远不会红**的测试是装饰，不是覆盖；留着一个假突变会让「6 个突变全红」变成**假话**。这是「没有红过的检查器不是证据」的硬币背面 —— **也没法被红过的，同样不算证据**。

**CRLF 轴为何天然无险**（实测，非推理）：真实语料 **542/577** 个载体在本机是 CRLF、**0** 个含孤立 CR；守卫用 `splitlines()` + `line.strip()` ⇒ **天生 EOL 无关**。本机 `core.autocrlf=true` ⇒ 同一提交本机 CRLF、CI LF，实测两侧判词一致。

### 47 条 MALFORMED —— 另一个缺陷类，本票不修

守卫首跑就报 47 行「不是合法 JSON」，分布在 5 个 2026-06/09 归档任务（`06-24-multi-repo-config` 20 行、`09-14-t9-merge-barrier` 8、`09-15-t12-affinity-placement` 8、`09-14-t10-context-compiler` 7、`09-14-t11-env-allowlist` 4）。

**根因（实测）**：这些文件是**把 JSON 数组写进了 `.jsonl`**，不是损坏 —— 每行以 `,` 结尾（数组元素分隔符），**去掉行尾逗号后逐行 `json.loads` 全部通过**（9/9、8/8、8/8、4/4；`06-24` 两个文件整文件 `json.loads` 直接得到 `list`）。**⇒ 无数据丢失**，是格式约定错用。

**关键判据**：把这 6 个文件全部按上述方式恢复后取 `.trellis` 引用（`file` 与 `path` 两键）共 **2 条、悬空 0** ⇒ **本票的 14 不是漏计**，两个缺陷类**互相独立**。

**为何不在本票修**：本票治的是「引用必须可解析」，这 47 行的缺陷是「载体不是合法 JSONL」——**修法、判据、风险都不同**（动的是归档记录的内容格式）。按仓规「只记录测量与根因，不臆造」⇒ **记账，另裁**。**不为它加白名单**（同 DANGLING：白名单是把红关掉）。

### Main Changes

| 文件 | 改动 | numstat |
|---|---|---|
| `scripts/check-tasks-refs.py` | **新增**守卫（`git ls-files` ∩ `EXCLUDE_DIRS` 索引 + 跟踪集语料 + `--audit`/`--json`/`--root`） | +243 |
| `scripts/check-tasks-refs-selftest.py` | **新增**消融自检（6 突变 + 红集不相交 + 独立进程认证） | +242 |
| `tests/python/test_check_tasks_refs.py` | **新增** 14 用例（合成仓 + 真 CLI 子进程 + 真实语料基线） | +431 |
| `.github/workflows/ci-scripts.yml` | **新增独立 job `tasks-refs`**（4 步：ruff / 守卫 / pytest / 消融自检；**无 `paths`**） | +47/-0 |
| `.trellis/tasks/archive/2026-09/09-18-t30-…/{prd,implement,check,task.json}` | 任务目录（prd 含 6 条实现期实测更正；`task.json` 归档后手填 `commit`） | — |

**裁决（本票当场定，写明理由）**：
- **`DANGLING` 与 `MALFORMED` 都是结构性失败 ⇒ 退非零**（清单有限、能被 `archive_task_dir` 的自愈路径止血 ⇒ **红是可修的**）。
- **不设白名单**（`EXPECTED_DANGLING` 那是把红关掉，不是修它；14 条已在 #680 记账）。
- **basename-only 命中计为悬空**（路径写法错误，与 `check-spec-refs.py` 的 `PATH_FORM` 同类），不是 advisory。
- **CI 不加 `paths`**：守卫读**开放输入集**（任何归档目录、任何被引文件都能翻判词），过滤会**蒙住**它 —— 与 `ci-scripts.yml` 既有的 `spec-refs` 同形。

### Testing

| 检查 | 结果 |
|---|---|
| `scripts/check-tasks-refs.py`（真实语料） | `787 refs / 14 dangling / 47 malformed`，**从干净 `worktree` 复算同值** |
| `python scripts/check-tasks-refs-selftest.py` | **SELF-CHECK PASSED**：6 突变全红、红集两两不同、恢复 sha256 `a74deb2a…` 独立进程认证 |
| `pytest tests/python/test_check_tasks_refs.py` | **14 passed** |
| `pytest …test_check_tasks_refs.py …test_check_spec_refs.py` | **26 passed**（两个守卫测试同跑，互不影响） |
| Python 套件收集数 | **1196 → 1210** = **+14**，与本票新用例数**恰好相等** ⇒ 无附带改动 |
| `ruff check`（0.16.3，三个新文件） | All checks passed |
| `ast.parse(feature_version=(3,9))` | 三个文件全通过 |

### Delivered

- `2f88e44` `feat(scripts): guard .trellis/tasks jsonl references (#680)`（**本地**，见下）
- 归档：`.trellis/tasks/archive/2026-09/09-18-t30-tasks-jsonl-ref-guard/`（`task.json` 已手填 `commit=2f88e44`）

### 未完成 / 阻塞（诚实记账）

⚠️ **`2f88e44` 未推送**。`git push origin main` 两次失败：

```
fatal: unable to access 'https://github.com/JameryW/UltimateCoders.git/':
Failed to connect to github.com:443 over proxy 127.0.0.1 after 2048 ms
```

`git ls-remote origin refs/heads/main` **同样失败** ⇒ **远端仍为 `37509c3`，本地领先 1**。⇒ **不能写「已推送」**：本仓铁律是「『已推送』只能由 `git ls-remote` 回读背书」。**CI 未跑**（无网）⇒ **`2f88e44` 的 CI 结论未知**，按仓规**实现票必须先 CI 绿再关票** ⇒ **#680 暂不关**。

### Next Steps

1. **网络恢复后**：`git push origin main` → `git ls-remote` 回读确认 `2f88e44` → 等 CI（新 job `tasks-refs` 在 3.9/3.12 两档）→ 绿后关 #680（贴**验收映射**）。
   ⚠️ **预期 `tasks-refs` job 是红的**：它跑守卫，而 14 悬空 + 47 malformed **仍在**。**这是守卫在说真话，不是守卫坏了** —— 需在 #680 的关票评论里写明，或另开票处置那 47 条。
2. **47 条 MALFORMED 的处置**（另一个缺陷类，本票只记账）—— 判据与修法已写在 prd.md「实现期实测」第 5 条。
3. **14 条悬空（#678 的 B 类）** —— 其中 2 条是**新形状**（退役 `JameryW/workspace` 身份目录 / 归档时目录套目录），判定规则不唯一 ⇒ **机械回填会写错**，须单独裁。
4. **#656 P2 本体** —— 仍是唯一**外部阻塞**项（需「方案第 21 节」原文）。

### Git Commits

| Hash | Message |
|------|---------|
| `2f88e44` | `feat(scripts): guard .trellis/tasks jsonl references (#680)` |

> ⚠️ 本地提交，**未推送**（网络不通；详见上一节「未完成 / 阻塞」）。
### Status

[OK] **本地已完成** —— 守卫 + 自检 + 14 用例 + CI 接线全部落地并实测；⚠️ **未推送、CI 未跑 ⇒ #680 暂不关**。


## Session 36: T31 / #681 — clean .trellis/tasks jsonl residue, guard turns green

**Date**: 2026-09-18
**Task**: T31 / #681 — clean .trellis/tasks jsonl residue, guard turns green
**Branch**: `main`

### Summary

交付 **T31 / #681**：把 `.trellis/tasks` 的 jsonl 存量欠账清到 `0 dangling / 0 malformed`，
让 T30 交付的守卫（`scripts/check-tasks-refs.py`）**在 CI 里转绿**。

### Main Changes

- **47 条 malformed 不是一种形状，是两种**（一手分类，0 条 unclassified）：
  - **27 行**（4 文件）每行是合法 JSON 对象、**行尾多一个逗号** ⇒ 删那一个字节即可。
  - **18 条**（2 文件）整文件是 `[ {..}, .. ]` **JSON 数组**，键是 `path` 不是 `file`
    ⇒ 拆成 JSONL，**并有意保留 `path` 键名**，免得这些历史条目悄悄进入守卫的 `file` 计数。
- **14 条 dangling 不是一种成因，是六种**：
  - 9 条指向归档前的旧路径（任务目录搬移）；
  - 1 条少了一层同名目录（`06-22-rust-scheduler/06-22-rust-scheduler/prd.md`）；
  - 2 条写成 `.trellis/spec/backend/type-safety.md`，实际在 **`frontend/`**；
  - 3 条指向**全仓从未存在**的目标（`.trellis/spec/backend.md`、
    `.trellis/spec/backend/workspace-config-spec.md` ×2）⇒ **删引用，不臆造路径**。
  - ⇒ **11 条 repoint + 3 条删除 = 14**。
- **每个 repoint 目标都先用 `git ls-files` 验明存在**才动手（不猜）。每个文件**保持自己的行尾**
  （两个 06-24 文件是 CRLF，改完仍是 CRLF）。
- 数字闭环：**788 → 785 引用（= -3，恰为删除数）**，`0 dangling / 0 malformed`，守卫 exit 0。
- `test_real_corpus_reproduces_the_recorded_numbers` **同票更新**（它是有意钉住数字的绊线）。

### Testing

- `scripts/check-tasks-refs.py` ⇒ `785 ok / 0 dangling / 0 malformed`，**exit 0**。
- `check-tasks-refs-selftest.py` ⇒ **SELF-CHECK PASSED：6 突变、红集两两不同**，
  守卫 sha256 仍是 `a74deb2a…`（**未被本票改动**）。
- `tests/python/test_check_tasks_refs.py` ⇒ **14 passed**。
- 全量收集 **1210**（与改前一致 —— 本票只改数据文件、不新增用例）。
- 其余守卫：spec-refs **0**、journal **0**。

### CI

- **`Scripts CI` 的 `tasks-refs` job 全绿**（3.9 + 3.12 两版，含 `run the guard`、
  `real corpus tests`、`mutation self-check`）—— 这是本票的验收核心。
- 此前 `Scripts CI` 在 `1323cc00` 是红的（守卫**如实**报出 14+47）；本票把它转绿。

### Git Commits

- `3305504` fix(trellis): clean .trellis/tasks jsonl residue (0 dangling / 0 malformed) (#681)
- `ad5c06c` test(trellis): widen the corpus pin to cover this ticket's own two jsonl files (#681)

### 撞到的坑（值得记住）

1. **我自己写的探针脚本也算「间接信号」。** 我用一个自算脚本得到「43 条 trailing-comma」，
   而守卫说 47；`tail -20` 把表截断了，我**读错了半行**。改用守卫自己的语义重算 ⇒ 两处都是 47。
   ⇒ **判据必须以被审对象的语义复算，不是拿自己的近似脚本当准**。
2. **EOL 回归是我自己引入的**：第一版 `rewrite()` 把两个 CRLF 文件写成了 LF。
   回滚后用**「逐文件实测 + 混合即 abort」**的 `measure()` 重做。
   ⚠️ 更早我还**误报过这两个文件是 LF** —— 探针里 `crlf`/`lf` 的算法写错了。
3. 🔴 **钉数字的绊线，必须钉「CI 会看到的那棵树」。**
   我先删了存量、在**未提交**的树上读到 **785**，就把 pin 写成 `(784, 785)`；
   而 CI 跑的是**已提交**的树 —— 我这张票自己的两个 jsonl 已被跟踪、各贡献 1 条引用 ⇒ **787**。
   **CI 直接把它打红**，我才发现。修法 = 放行 `(785, 786, 787)` 窗口（785 无我票文件 / 787 有）。
   ⇒ **本地读数与 CI 读数属于不同的树；钉数字前先想清楚「CI 会在哪棵树上跑」。**

### Next Steps

- 无。本票范围已闭合；守卫转绿即验收。
- **#656 P2 本体**仍阻塞于外部「方案第 21 节」原文（唯一外部阻塞项）。


### Status

[OK] **Completed** -- guard green; `Scripts CI` `tasks-refs` job passing on 3.9 + 3.12


## Session 37: 架构升级收口审计（五面）—— 迁移程序已完成；唯一未竟是 P2 本体待外部原文；§六 覆盖边界放宽至 T21–T31

**Date**: 2026-09-18
**Task**: 架构升级收口审计（五面）—— 迁移程序已完成；唯一未竟是 P2 本体待外部原文；§六 覆盖边界放宽至 T21–T31
**Branch**: `main`

### Summary

五面复核：迁移程序已全部完成；P2 本体待外部「方案第 21 节」原文（外部阻塞，非欠账）；同票修 §六 覆盖边界

### Main Changes

本轮用户问「架构升级任务是否全部完成」。按四面法（地图 / 未归档目录 / 文档进度尾 / 已关闭票的 carry-over）
加第五面（引用完整性）逐一手复核：

**面① 地图 issue**：开放地图**仅 #656（P2）**；#632（P0）、#644（P1）已关，**D4–D16 全部已裁并关**。
#656 正文尾段与其唯一评论（2026-09-18）口径一致：**开放决策 0、待落地票 0**。

**面② 未归档目录**：`ls .trellis/tasks/` 与 `task.py list` **双视图均为 0** ——
无「没有 `task.json` 的幽灵目录」。

**面③ 文档进度尾**：判据是 `task.json` 的 `map` 字段，**不是编号连续**。
实测 `map = 656` 的只有 **T16 / T17 / T18 / T19**（4 张），其余 **290 张为 `None`**
⇒ **T30 / T31 同属框架卫生线**，不挂在迁移程序上。
⚠️ **本轮唯一真欠账**：评估件 §六 的覆盖边界只写到「T21–T29」，未含 T30 / T31
⇒ 已同票放宽为 **T21–T31**，并补上两票的 issue 号与用途；P2 状态行的日期也一并刷新。

**面④ 已关闭票的 carry-over**：#644 关闭评论里三条「残余风险」在 #656 上**各有落点**
（2026-09-18 补记，逐条一手核对）—— 1（live NATS e2e）**收窄**为「仅网关侧一半」、
2（`ALL_AGENTS` 清单漂移）成立、属 P2 开票输入、3 为环境事实 ⇒ **无悬空交接项**。

**面⑤ 引用完整性**：三道守卫全绿 —— `check-tasks-refs` **787 ok / 0 dangling / 0 malformed**、
`check-spec-refs` **0 structural failure**、`check-journal-ledger` **36/36 conforming / 0 placeholder**。

**结论**：迁移程序**已全部完成** —— P0 / P1 关闭、D4–D16 全裁、挂在 #656 上的 T14–T19 全交付并归档。
**「未完成」的部分是 P2 本体无据**（Optimizer 算法 / review 策略 / market scheduling），
需外部「方案第 21 节」原文 ⇒ **属外部输入阻塞，不是欠账**，本件不臆造。

**方法上值得记的**：#656 正文里「开放决策」出现 10 次，但**全是历史进度条目**
（正文自己就标了「此句已过期，不要据它判断当前状态」）。
⇒ **词频是间接信号**；判当前状态只能读**最新一段**。


### Git Commits

| Hash | Message |
|------|---------|
| `fe848635` | (see git log) |

### Testing

- 三道守卫全绿：`check-tasks-refs` **787 ok / 0 dangling / 0 malformed**（exit 0）；
  `check-spec-refs` **0 structural failure**；`check-journal-ledger` 修复后 **37/37 conforming / 0 placeholder**。
- 评估件改动 `git diff --numstat` = **2/2**（小改动，非整文件重写），文件仍是 **CRLF-only**。
- 零代码变更 ⇒ **Rust / Python CI 不会触发**（本轮只动 `docs/**` 与 `.trellis/**`）。

### Status

[OK] **Completed**

### Next Steps

- 无。本轮结论是「迁移程序已完成」；唯一未竟是 **#656 P2 本体**，阻塞于外部「方案第 21 节」原文。
- ⚠️ **记一条自伤（本轮已修）**：上一轮回滚 journal 时只把 `index.md` 的计数改回 36，
  **没有改那个 session 标题的编号** ⇒ 本轮脚本按 36 递增，产出**第二个** Session 37。
  守卫当场报 `SESSION_NUMBER: session 37 is used twice` —— 已把前一个（T31 交付）改回 **36**。
  ⇒ **回滚 journal 必须连「标题编号」一起回滚，不只是 `index.md` 的计数**（§5.34 ④ 的补强）。


## Session 38: P2 开票前置勘察：分离「仓内已确证」与「待 §21」

**Date**: 2026-09-18
**Task**: P2 开票前置勘察：分离「仓内已确证」与「待 §21」
**Branch**: `main`

### Summary

P2-1/2/3 的仓内前置与「只有方案第 21 节能回答的问题」分列成文（durable-runtime-p2-recon.md），不臆造本体

### Main Changes

按「继续」自主推进。上一轮一手核实：迁移程序已完成、无下一票可取（开放 issue 仅 #656、
0 活动任务、0 开放 PR、CI 绿），唯一未竟是 #656 的 P2 本体，阻塞于外部「方案第 21 节」原文。
本轮不去空等，而是把这个外部阻塞的**时滞**压到最短 —— 原文一到即可开票。

- 新增 `docs/architecture/durable-runtime-p2-recon.md`：把 P2-1 / P2-2 / P2-3 的
  **仓内已确证前置**（每条带 `file:line`，2026-09-18 实测）与**只有 §21 能回答的问题**
  分列两栏；后者一律留空位，**不臆造**。
- P2-1：指标列 `cost` / `tokens` / `duration_ms`（`graph_store.rs:928-930`）；
  T14 #659 写入点、T15 #660 上报契约（同事务同条）、T18 #668 逐步用量 `payload["steps"]`。
- P2-2：T16 #661 让 `graph_nodes.type` 有写者（`node_type_for` 由 capability 派生）；
  T19 #670 的 `requires_independence` 是唯一条件、节点打标与派发口**共用**它；
  另记 D16 未解的 `steps[].agent` 命名冲突与 T19 的残余 race 窗口（**已记账，非欠账**）。
- P2-3：仓内**零坐实** —— `PlacementScore` 0、`Execution Optimizer` 0、`market` 仅 3 处
  无关命中 ⇒ 评估件 2026-09-15 那句「范围无法在仓内坐实」**至今成立**（顺手复测，
  防「过期规格自带权威感」）。
- 评估件 §六 的 P2 状态段后加一句指向该文，避免孤儿文档（numstat **1/0**）。

边界：本文只列事实与空位，**不是**本体设计、**不是**决议票；开票时以票面 Acceptance 为准。


### Git Commits

| Hash | Message |
|------|---------|
| `03de685` | docs(architecture): 记录 P2 开票前置勘察，分离「仓内已确证」与「待 §21」 |

### Testing

- [OK] 锚点复核：`git grep -n` 定位后逐段读原文 —— `graph_store.rs:928-930`（指标列）/ `:1845-1849`（`EventUsage`）/ `:2660` + `:1936-1938`（`steps_payload`）/ `:352-358`（`node_type_for`）/ `:381-383`（`requires_independence`）；`worker_service.rs:319-324`（`dispatch_gate`）/ `:440-447`（`placement_target`）；`test_sandbox_env_allowlist.py:51`（`ALL_AGENTS`）
- [OK] `git diff --numstat` 评估件 **1/0**：纯新增一句指向，既有段落未动
- [OK] 新文档 CRLF-only、5716 B，无反斜杠泄漏；本次**无代码变更**，四面守卫的输入不受影响

### Status

[OK] **Completed**

### Next Steps

- 外部「方案第 21 节」原文到达后：按 `durable-runtime-p2-recon.md` 的空位逐条填答 → 开 P2-1 / P2-2 / P2-3 的**决议票**（编号开票时确认）。
- 开票时把两笔**已记账残余**一并裁决：#644 残余 2（`ALL_AGENTS` 清单漂移）与 T19 的共享队列 race 窗口 —— 二者都不是欠账，是 P2 的输入。
- 本轮两处自伤（均被断言拦在写盘前）：① 多行替换用 `\n` 会把 lone LF 注入 CRLF 文件；② 检查占位符时扫**整文件**会因早先 session 讨论过占位符原文而**自指误报** ⇒ 判据要限定在本次 session 内。


## Session 39: T32：sandbox allowlist 测试的 agent 清单改为从 allowlist 自身推导

**Date**: 2026-09-18
**Task**: T32：sandbox allowlist 测试的 agent 清单改为从 allowlist 自身推导
**Branch**: `main`

### Summary

消除测试侧手抄清单与 ADAPTER_ENV_ALLOWLIST 的平行维护：改为推导式 + 元测试钉住；消融两方向对照实证

### Main Changes

按「继续」自主推进。上一轮我留了一句「要不要开 #644 残余 2，你说了算」；本轮不等回答，
按约定**自己定并写明假设**：`ALL_AGENTS` 清单漂移是今日一手实测确证的覆盖缺口、
且**不依赖**外部「方案第 21 节」 ⇒ 属框架卫生线（与 T21–T31 同类），可独立开票交付。

## 交付（T32 / #682）

issue **#682** → 任务目录 `09-18-t32-env-allowlist-agent-derivation` → 实现 → 归档 → 本 session。

### 漂移面（实测）

`tests/python/test_sandbox_env_allowlist.py:51` 的 `ALL_AGENTS` 是**手抄清单**，与
`python/ultimate_coders/agent/sandbox.py:80` 的 `ADAPTER_ENV_ALLOWLIST` **平行维护**：
allowlist **6** 个键（grok-build / claude-code / claude-code-decompose / codex /
deepseek-harness / local-harness），测试清单 = 这 6 个 + `grok`（别名）+
`some-external-plugin`（刻意未知）= **8**。
⇒ 新增 adapter（= 加一个键）时参数化**不自动纳入**，新适配器的
「host secret 不得进入子进程」断言**静默缺失而套件全绿**。与 T13 教训同源。

### 修法

`ALL_AGENTS` 改为从 allowlist **自身推导**：allowlist 的键 ∪ `GROK_AGENT_ALIASES`
中未直接命中的别名 ∪ 刻意未知项。别名那一支**保留**（它覆盖「别名须经 registry
归一后才命中 allowlist」的路径）；`some-external-plugin` 提为 `UNKNOWN_AGENT` 常量
并消掉另外三处字面量；新增**元测试**把「清单必须保持推导」钉住。
**非目标**：不改 allowlist 内容；不用 `available_agents()` —— 它的 `discover_once()`
会让参数化集合依赖宿主环境，破坏「总收集数对账」这条回归判据。

## 验收（四条全过，均一手实测）

| # | 判据 | 实测 |
|---|---|---|
| 1 | 参数化条目 8 ⇒ 8 | 推导结果 = 原 8 个（顺序略变、语义一致） |
| 2 | 消融：注入探针键 | **修复前 69 不变**；**修复后 70 ⇒ 72**（两个参数化点各 +1），测试文件一字未动 |
| 3 | 该文件 pytest | **70 passed**（基线实测 69，+1 = 新增元测试） |
| 4 | Python 总收集 | **1210 ⇒ 1211** |

`ruff check` 通过；消融后两文件均按字节恢复、sha256 一致；四个 blob 全 LF-only
且与工作树逐字节一致（**CI 会看到的那棵树 = 我测的那棵树**）。

提交：`579843e`（实现）+ `aa6898d`（归档）。

## 值得记的

- **消融必须两方向对照**：只证「修好后能自动纳入」不足以说修好了 —— 用**修复前版本 +
  同一注入**跑出 69 不变，才把「正是那个失效模式」变成实测事实（与「没有红过的检查器
  不是证据」同源）。
- **开新票会移动被钉死的语料计数**：`.trellis/tasks/<新票>/*.jsonl` 里的 `.trellis` 引用进语料 ⇒
  `test_real_corpus_reproduces_the_recorded_numbers` 立刻红，**Scripts CI 与 Python CI 同时红**。
  这不是回归，是该测试 docstring 规定的「同 change 更新」；T31 的 `ad5c06c` 做过同样的事，我漏了。
  顺手把 T31 的**增长窗口 `(785,786,787)` 收紧成可达对 `(787,788)`** —— 窗口里的陈旧值会吞掉真实的 -1 漂移。
- **突变自检不能被「打断」**：前台跑 `check-tasks-refs-selftest.py` 撞默认超时被 SIGTERM ⇒ 在
  `scripts/check-tasks-refs.py` 里**留下一处突变**（`posix in index` → `(ROOT/posix).exists()`）。
  症状极具误导性：**一个与本票无关的测试**变红（`test_untracked_target_does_not_resolve`），
  而半小时前同一命令是 14 passed。判据：**同一命令前后读数不一致 ⇒ 先怀疑工作树变了**，
  拿 blob 哈希比对（工作树 `dffb2325` vs HEAD `a74deb2a`），而不是先怀疑自己刚改的代码。⇒ 一律后台跑。
- `task.json` 的 `commit` 字段仍是**空的**（`task.py archive` 不写它）⇒ 归档后手填
  `579843e`；`implement.jsonl` 的 prd 引用由脚本自动改指归档路径，**已通读确认**。


### Git Commits

| Hash | Message |
|------|---------|
| `579843e` | test(python): derive the sandbox allowlist sweep from ADAPTER_ENV_ALLOWLIST (#682) |
| `aa6898d` | chore(task): archive 09-18-t32-env-allowlist-agent-derivation |
| `6f8d50f` | test(trellis): re-pin the task-reference corpus at 787/788 (#682) |

### Testing

- [OK] `pytest tests/python/test_sandbox_env_allowlist.py` → **70 passed**（基线实测 69，+1 = 新增元测试）；`ruff check` 通过
- [OK] 消融**两方向**：修复前 + 探针键 = **69 不变**（正是那个失效模式）；修复后 + 探针键 = **70 ⇒ 72**（两个参数化点各 +1），测试文件一字未动；两文件均按字节恢复、sha256 一致
- [OK] Python 总收集 **1210 ⇒ 1211**；四个 blob 全 LF-only 且与工作树逐字节一致（CI 看到的树 = 我测的树）
- [OK] `check-tasks-refs` 语料 **788 ok / 0 dangling / 0 malformed**；`test_check_tasks_refs.py` **14 passed**（含 re-pin 后那条）；`ruff` 通过
- [OK] re-pin 消融：往归档 jsonl 注入一条目标**存在**的额外引用 ⇒ `reference count drifted: 789` 报红；按字节恢复后由独立进程复算 sha256 一致

### Status

[OK] **Completed**

### Next Steps

- 实现票 T32 已直落 main（`579843e` 实现 + `aa6898d` 归档）；**待 CI 绿后关 #682**（贴四条验收映射）。
- #656（P2 本体）仍只等外部「方案第 21 节」原文 —— 本轮 T32 **不在**该依赖内，故可独立交付。
- 若再遇「无票可取」：先按 §5.51 判「确实无票」还是「有可自推的独立欠账」（本轮即后者，判据是靠**实测**漂移面存在）。


## Session 40: T33 pin the WorkerService RPC -> roster field mapping (#683)

**Date**: 2026-09-18
**Task**: T33 pin the WorkerService RPC -> roster field mapping (#683)
**Branch**: `main`

### Summary

补一条穿过 RPC 边界的 placement 钉：三处字段映射（recent_files/per_worker_topic/capabilities）此前全部无测试，三条突变各自存活 249 passed

### Main Changes

**T33 / #683** —— 钉住 `WorkerService` 两个 RPC 到花名册的字段映射（框架卫生线，不依赖外部「方案第 21 节」）。

## 起点：接缝无主（一手实测）

`placement_target` 读的**每一个**输入都是从 wire 进来的：心跳的 `recent_files` / `per_worker_topic`、
注册的 `capabilities`。两个 handler 都逐字把它传给了 `register_with_projects` / `heartbeat_with_signals`
（我逐行读过，当前**都是对的**）。问题是**没有测试钉住这件事**：

- 7 个 placement 测试（`server.rs:8342/8359/8392/8420`、`worker_service.rs:2101/2121/2139/2180/2260/2422`）
  全部用**本地辅助**直接戳 registry —— `registry_with`（`server.rs:8300-8313`）、`signalled`（`worker_service.rs:2019-2035`）；
- 3 个 `register_worker_rpc_*`（`:1388` / `:1414` / `:1996`）走到 RPC，但只断言「接受 / 拒绝」，
  **从不要求花名册做决策**。

⇒ 与 T12（软语义让「功能死了」= 「没理由定向」）、T32（手抄清单与真源平行维护）**同一个形状**。

## 消融（3 处独立突变，`cargo test -j 1 -p uc-grpc --all-features`）

| 突变 | 重编译 | 结果 |
|---|---|---|
| `req.per_worker_topic` → `false` | `Compiling uc-grpc`×1 | 249 passed / **0 failed（存活）** |
| `&req.recent_files` → `&[]` | ✅ | 249 passed / **0 failed（存活）** |
| `req.capabilities` → `Vec::new()` | ✅ | 249 passed / **0 failed（存活）** |

**阳性对照**：`PER_WORKER_SUBJECT_PREFIX`（`placement.rs:249` 的绝对钉）加 `zz` ⇒ **rc=101 / 6 failed**
⇒ 装置确有检出能力；基线 rc=0（lib 249 + 集成 8）。三次突变后按字节恢复，sha256 回 `4b6a4db8…`，工作树 0 行。

**反例（顺带发现，不在本票范围）**：`placement.rs:303` 的 `assert_eq!(norm.len(), MAX_RECENT_FILES)`
是**符号自指** —— 常量 `64 → 640` 后仍全绿，它钉不住自己的值。proto 未承诺该数字，故记录不修。

## 交付与验收（四条全过，均一手实测）

新增 `placement_signals_survive_the_rpc_boundary`：真 proto 请求 `register_worker` + `worker_heartbeat`
→ `placement_target` 决策 → 断言**字面量** subject（符号断言会随常量漂移，钉不住）。

| # | 判据 | 实测 |
|---|---|---|
| 1 | 新测试未突变 | **绿**（`test worker_service::tests::placement_signals_survive_the_rpc_boundary ... ok`） |
| 2 | m1 / m2 / m3 各自单独施加 | **三条各自恰好打红新测试**（`249 passed; 1 failed`，失败名就是它）；修复前 **0/3** 红 |
| 3 | `cargo test` lib 计数 | **249 ⇒ 250 passed**，0 failed |
| 4 | 语料钉值 | `(787, 788)` ⇒ **`(788, 789)`**；`check-tasks-refs` 未跟踪 **788** / 已跟踪 **789**，均 0 dangling |

`cargo fmt --all --check` 干净；`cargo clippy -j 1 --workspace --all-targets --all-features -- -D warnings`
**RC=0**；`pytest tests/python/test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**。

## 值得记的

- **「两半各测各的」也是一种沉默漂移**：单侧各自绿（7 + 3 个测试）并不能覆盖**接缝**。
  判据很硬：把 handler 的映射改错，套件会不会红 —— 不会，就说明接缝无人守。
- 🔴 **本地跑 `check-tasks-refs-selftest.py` 必然中断**（本轮首次定位到确切形状）：pytest 会话末清
  `%TEMP%\pytest-of-jamer\garbage-*`（实测 **count=968** > 阈值 50）撞 safe-delete ⇒ pytest 打印完
  `..............`（14 个点全过）却以 rc=1 退出且**没有 summary 行** ⇒ 自检 `no summary ⇒ raise` 的
  fail-closed 把它变成崩溃。**把 `TEMP` 指到新目录也没用**（pytest 仍自建 `garbage-*` 且同样超阈值）。
  ⇒ 它不是「判定红」，是环境现象；**本地只拿得到中间读数**（本轮拿到 `baseline red set = none` +
  前两条突变打红**互不相交**的集合 ⇒ 判别力已证），完整结论只能由 CI 给。
- **阳性对照可能自己就是反例**：第一版对照改 `MAX_RECENT_FILES` 想让它红，它绿了 —— 因为那条断言
  是符号自指。**对照必须落在有绝对期望的点上**，否则「装置没检出能力」会被误读成「装置正常」。
- **装置要先证明会红，再相信绿**：三个突变都带 `Compiling uc-grpc` 证据（否则「绿」可能是没重编译）。
- `task.py archive` **不写 `commit`** —— 归档后通读确认 `commit: null` 并手填 `82ab716`；
  脚本重写 `task.json` 时**丢掉了末尾换行**（与 T32 的 26 CRLF + 有换行不同形），已补回。


### Git Commits

| Hash | Message |
|------|---------|
| `82ab716` | test(uc-grpc): pin the WorkerService RPC -> roster field mapping (#683) |
| `b895a7d` | chore(task): archive 09-18-t33-rpc-to-roster-passthrough-pin |

### Testing

- [OK] 新测试未突变源码下**绿**；`cargo test -j 1 -p uc-grpc --all-features` lib `249 ⇒ 250 passed`，0 failed
- [OK] 突变自检：m1(`per_worker_topic`→`false`) / m2(`&req.recent_files`→`&[]`) / m3(`req.capabilities`→`Vec::new()`) **各自单独施加都恰好打红新测试**（`249 passed; 1 failed`，失败名即它）；修复前 **0/3** 红；每次均带 `Compiling uc-grpc` 证据，恢复后 sha256 回 `4b6a4db8…`、工作树 0 行
- [OK] 阳性对照 `PER_WORKER_SUBJECT_PREFIX` + `zz` ⇒ **rc=101 / 6 failed**（装置确有检出能力，绿不是「没重编译」）
- [OK] `cargo fmt --all --check` 干净；`cargo clippy -j 1 --workspace --all-targets --all-features -- -D warnings` **RC=0**
- [OK] `check-tasks-refs` 未跟踪 **788** / 已跟踪 **789**，均 **0 dangling / 0 malformed**；`test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**
- [OK] `check-journal-ledger.py` 通过（本账本 40 session(s)、0 占位符）
- [WARN] `check-tasks-refs-selftest.py` **本地跑不完**：pytest 会话末清 tmp（`count=968` > 阈值 50）撞 safe-delete ⇒ rc=1 且无 summary ⇒ 自检 fail-closed 中断。已取中间读数（`baseline red set = none` + 前两条突变打红**互不相交**集合），完整结论以 CI 为准

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`82ab716` 实现 + `b895a7d` 归档）；**待 CI 绿后关 #683**（贴四条验收映射）。
- 本票只补钉、未动生产代码；`placement.rs:303` 的符号自指（`MAX_RECENT_FILES` 钉不住自己的值）**已记账、刻意未修**。
- #656（P2 本体）仍只等外部「方案第 21 节」原文；其上「已记账残余」现在只剩 T19 的 race 窗口一项。


## Session 41: T34 lint the whole scripts/ directory, retiring the name-files-only workaround (#684)

**Date**: 2026-09-19
**Task**: T34 lint the whole scripts/ directory, retiring the name-files-only workaround (#684)
**Branch**: `main`

### Summary

把 `scripts/` 变成一等 lint 面：修掉 `check-codex-issue-flow.py` 的两处既有 ruff 错误（`Found 2 errors` → `All checks passed!`），新增 `scripts-lint` job 跑 `ruff check scripts/`，并同票修掉那句被本票变成假话的文件头注释。判据性消融：同一突发下 CI 现有的三条点名文件命令**全绿**而目录级命令**变红** —— 覆盖是真的，且「新加进 `scripts/` 的文件天生无人 lint」这个类被消掉了。

### Main Changes

**T34 / #684** —— 修掉 T26 留下的两处既有 ruff 错误，退休「ruff 只能点名文件」的临时形状（框架卫生线，不依赖外部「方案第 21 节」）。

## 起点与判据

T33 收口后 `origin/main = dcaef15`、工作树干净，开放 issue 只剩 #656（阻塞于外部「方案第 21 节」原文）。本会话承接的是仓自检线（T26→T33 一路在做的「检查器 / 门禁卫生」），本轮问的是 **T26 当年那个临时形状留下了什么**。

## 一手测量：scripts/ 的 lint 面

`git ls-files '*.py'` ∩ 各 workflow 里 `ruff check` 的实参（2026-09-18 实测）：

| 目录 | tracked `.py` | 被某个 CI ruff target 覆盖 | 未覆盖 |
|---|---|---|---|
| `python/` | 36 | 36（`ci-python.yml:32` 目录级） | 0 |
| `tests/` | 51 | 51（同上） | 0 |
| `scripts/` | **5** | **4**（3 个在 `ci-scripts.yml:67/93-95`、1 个在 `ci-journal.yml:69`） | **1** |
| `.trellis/scripts/` | 28 | 1（`ci-trellis.yml:66`） | 27 |
| `.claude/hooks/` | 3 | 0 | 3 |
| **合计** | **123** | **92** | **31** |

`scripts/` 上唯一逃逸的是 `scripts/check-codex-issue-flow.py` —— 正是 `ci-scripts.yml:19-21` 逐字记下的那两处既有 ruff 错误（`I001` + `UP045`，末次改动 `fc9b5ce`）。

**形状问题比那两行大**：只要 ruff 步骤仍点名文件，**下一个**加进 `scripts/` 的守卫就同样天生无人 lint。T34 要消的是这个类。

## 为什么这是欠账而不是已决冻结

| 出处 | 逐字 | 判读 |
|---|---|---|
| T26 `prd.md:113` | `**不**修 ... 的两处既有 ruff 问题（不在本票）。` | **非目标** —— 本票不做 |
| T26 `prd.md` 的 `## 遗留（不在本票）` | 列了 2 项 | **不含**这两处 ⇒ 既非遗留亦非冻结 |
| `ci-scripts.yml:21` | `and this workflow owns neither` | **没人拥有**，不是「决定永不修」 |

## 变更

1. `scripts/check-codex-issue-flow.py`：`I001`（import 块后两个空行 → 一个）+ `UP045`（`-> Optional[str]` → `-> str | None`，并删掉随之未使用的 `from typing import Optional`）。形状取自 `ruff check --fix --diff` 的逐字输出，非推测。**3772 → 3738 B**，CRLF 104 → 102，行为不变（`rc 0`、输出逐字同）。
2. `.github/workflows/ci-scripts.yml`：新增第三个 job `scripts-lint` 跑 `ruff check scripts/`；**既有两个 job 的 `steps` 逐字未动**（`yaml.safe_load` 后与 HEAD 比对相等）；workflow 无 `paths`（本来就无）。
3. 同票修掉文件头那句**已被本票变成假话**的注释（`ruff check scripts/ ... is red today`），并写清「为什么这里是目录级、另两处仍点名文件」与「为什么这个 job 在本文件而不是另开 workflow」。
4. `README.md` / `README.zh-CN.md` 的 Lint 配方补 `scripts/`（各 +9 B）—— 同一处遗漏的文档面。
5. `tests/python/test_check_tasks_refs.py` 语料钉值 `(788,789)` → `(789,790)`（本票 `implement.jsonl` 进语料）。

## 消融（两方向，均实测）

方向一 —— 新形状**能红**：

| 突变 | `ruff check scripts/` |
|---|---|
| M0 基线 | `rc=0` GREEN `All checks passed!` |
| M1 `scripts/` 下**新增**一个含 `F401` 的文件 | `rc=1` RED `F401 ... imported but unused` |
| M2 还原 `UP045` | `rc=1` RED `UP045 Use X | None` |
| M3 还原 `I001` | `rc=1` RED `I001 Import block is un-sorted` |

方向二 —— 旧形状对同一突发**是瞎的**（本票的判据性消融）：同一份 M1 下，CI 今天实际在跑的三条命名文件命令**全部 rc=0 GREEN**，而 `ruff check scripts/` **RED**。判词不同 ⇒ 本票**确实增加了覆盖**，不是装饰（T19 判据：两处突变打红同一集合 ⇒ 其中一条是装饰）。

突变全部按字节恢复，并由**独立进程**从磁盘复算 sha256 认证：`015a4feafee8455db579c4eef6fa708a19ff7d4f4219f160d751847cae7147fc`。

## 门禁与 CI

本地：`ruff check scripts/` **All checks passed**（改前 `Found 2 errors`）；守卫 `rc 0` 且输出逐字不变；两文件 `ast.parse(feature_version=(3,9))` 通过；`check-spec-refs` / `check-journal-ledger` 均 `rc 0` 未动；`test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**；workflow 3 job。

CI（`e6b2d03`）：**Scripts CI success**（含新 job `ruff lint (the whole scripts/ directory) -> success`，其 `ruff check scripts/` 步骤 `All checks passed!`）、**Python CI success**（4/4 job，`1203 passed, 8 skipped` 在 3.9 与 3.12 上逐字同基线）。`tasks-refs` 两个 Python 版本各自独立复算：**`789 ok / 0 dangling / 0 malformed`** —— 实现提交里 `task.json` 刻意不入库（仓规），任务目录仍未跟踪，故为 789；归档后该值为 790。


### Git Commits

| Hash | Message |
|------|---------|
| `e6b2d03` | ci(scripts): lint the whole scripts/ directory, not three named files (#684) |
| `c1d8940` | chore(task): archive 09-18-t34-scripts-lint-surface |

### Testing

- [OK] `ruff check scripts/` **All checks passed!**（改前 `Found 2 errors`）；`scripts/check-codex-issue-flow.py` 3772 → 3738 B（CRLF 104 → 102），行为不变：`rc 0`、`Codex issue workflow validation passed.` 逐字同
- [OK] 消融方向一（新形状**能红**）：M0 基线 GREEN；M1（`scripts/` 下**新增**一个含 `F401` 的文件）/ M2（还原 `UP045`）/ M3（还原 `I001`）**各自单独施加都 RED**。突变全部按字节恢复，由**独立进程**从磁盘复算 sha256 = `015a4feafee8455db579c4eef6fa708a19ff7d4f4219f160d751847cae7147fc`
- [OK] 消融方向二（**判据性**：旧形状对同一突发是瞎的）：同一份 M1 下，CI 今天实际在跑的三条命名文件命令（`ci-scripts.yml:67` / `:93-95` / `ci-journal.yml:69`）**全部 rc=0 GREEN**，而 `ruff check scripts/` **RED** ⇒ 判词不同，本票确实增加了覆盖，不是装饰
- [OK] `yaml.safe_load` 通过，job 数 **2 → 3**；`spec-refs`（6 步）与 `tasks-refs`（7 步）的 `steps` 与 HEAD **逐字相等**（既有 job 未动）；workflow 级无 `paths`
- [OK] 两文件 `ast.parse(feature_version=(3,9))` 通过；两个 README 行尾仍 **CRLF-only**，各 +9 B；`--numstat` 均小改动、无行尾翻腾
- [OK] `test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**；`check-spec-refs` / `check-journal-ledger` 均 `rc 0` 未动
- [OK] **CI（`e6b2d03`）**：Scripts CI **success**（含新 job `ruff lint (the whole scripts/ directory)`，其 `ruff check scripts/` 步骤 `All checks passed!`）；Python CI **4/4 success**，`1203 passed, 8 skipped` 在 3.9 与 3.12 逐字同基线
- [OK] `tasks-refs` 两个 Python 版本各自独立复算 **`789 ok / 0 dangling / 0 malformed`** —— 实现提交里 `task.json` 刻意不入库（仓规）故任务目录未跟踪，值为 789；归档后为 790 ⇒ 两个可达态都落在钉值 `(789,790)` 内

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`e6b2d03` 实现 + `c1d8940` 归档）；`e6b2d03` 上 Scripts / Python CI 全绿，**#684 待关**（贴验收映射）。
- 新增的 `scripts-lint` 是本仓第一个**目录级** ruff target；既有两个 job 仍**点名文件**（有意：它们的绿要自包含）—— 理由落在 `ci-scripts.yml` 文件头。
- **记账未做**：`scripts/check-codex-issue-flow.py` 零个 workflow 运行它（一个 CI-safe 的 wiring 守卫无武装，漂移对每个门禁不可见）⇒ 是否接进门禁需另开票。
- `.trellis/scripts/**`（27 处）与 `.claude/hooks/**`（3 处）仍不在任何 ruff target 内 —— **已登记排除**（`ci-trellis.yml` 头 / `.trellis/.template-hashes.json`），不是欠账。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。


## Session 42: T35 arm the Codex issue-flow wiring guard (#685)

**Date**: 2026-09-19
**Task**: T35 arm the Codex issue-flow wiring guard (#685)
**Branch**: `main`

### Summary

接 T34 落下的账：把 scripts/check-codex-issue-flow.py 接进门禁。该守卫守 21 条被断言路径（对外 50 个 tracked 文件）却被 0 个 workflow 运行 —— 21 条断言此前是宣称的能力、零执行。新建独立 workflow ci-codex-flow.yml（单 job、零依赖、3.9/3.12 矩阵、paths = 封闭输入集 9 条），因为 paths 是 workflow 级而 ci-scripts.yml 必须保持不过滤。两方向消融都做：6 处结构突变各自变红且集合两两不相交；同一真仓突变下 5/5 既有命令判词不变、只有新命令翻红。

### Main Changes

接 T34 / #684 落下的一条账：「`scripts/check-codex-issue-flow.py` 零个 workflow 运行它，是否接进门禁需另开票」。本票把它接进门禁 —— 21 条断言之前是**宣称的能力、零执行**。

## 起点一手复核（不继承上一轮叙述）

| 检查 | 命令 / 依据 | 读数 |
|---|---|---|
| 日期 | `date` | `Sat Sep 19 08:35` |
| HEAD | `git rev-parse HEAD` | `8cae2fe` |
| 远端 | `git ls-remote origin main` | `8cae2fe`（回读，与本地相等） |
| 工作树 | `git status --porcelain` | 空 |
| 开放 issue | `gh issue list --state open` | **1**（#656，3 条评论，末次更新 `2026-09-18T13:45Z`，**仍无「方案第 21 节」原文**） |
| 归档最大票号 | `ls .trellis/tasks/archive/2026-09/` | 最大 `-t34-` ⇒ 本票 **T35** |
| issue 最大号 | `gh issue list --state all` | 684 ⇒ 本票 **#685** |
| P2 前置 | `docs/architecture/durable-runtime-p2-recon.md` | P2-1/P2-2 前置**已全部交付并关票**，P2-3 零落点 ⇒ **P2 本体无工可开** |

⇒ 唯一可推进的是**框架卫生线**（T26→T34 一路在做的「检查器 / 门禁卫生」），且**不依赖**外部「方案第 21 节」。

## 缺口的一手读数（HEAD `8cae2fe`）

| 检查 | 命令 | 读数 |
|---|---|---|
| 谁在 workflow 里提到该守卫 | `git grep -l check-codex-issue-flow.py HEAD -- .github/workflows/` | 仅 `ci-scripts.yml`，且**只在文件头散文**（讲它那两处 ruff 错误）—— **无 job 运行它** |
| 谁提到被守面 | `git grep -l -e '\.agents/' -e 'AGENTS.md' HEAD -- .github/workflows/` | **0 处** |
| 唯一索引含该面的守卫 | `git grep -n os.walk HEAD -- scripts/check-spec-refs.py` | `:327` `os.walk(ROOT)` ⇒ 索引含 `.agents/**`；但该守卫 `--audit` **设计上从不失败**（劝告式）⇒ 判词恒绿 |
| 模板登记 | `.trellis/.template-hashes.json`（323 条） | `.agents/skills` **51**、`AGENTS.md` **1**；`docs/agents/*.md`×4 与 `docs/workflows/codex-issue-flow.md` **均不在** |
| 接进 CI 是否生来就红 | 逐条比对 21 条被断言路径 vs `git ls-files` | **21/21 全部 tracked** ⇒ 干净 checkout 即满足，**无生来红风险** |

**`scripts/` 下其他 4 个守卫都有 job**（`check-spec-refs.py` → `ci-scripts.yml:spec-refs`；`check-tasks-refs.py` 与 `-selftest.py` → `:tasks-refs`；`check-journal-ledger.py` → `ci-journal.yml:journal-ledger`）⇒ **只有这一个没有**。这也解释了 T34 为什么只能把它写进账本、不能顺手接上。

## 结论：过滤是**对的**，而且必须**独立成文件**

| # | 结论 |
|---|---|
| A | 该守卫的输入集是**封闭的**：遍历 `REQUIRED_SKILLS`（14 项硬编码）与 `REQUIRED_FILES`（6 项硬编码），**从不 glob 目录** ⇒ 能翻转它判词的改动**恰好**是这 21 条路径（+ 守卫自身 + workflow 自身） |
| B | 这与 `spec-refs`（`os.walk` 全仓）、`tasks-refs`（任意归档目录都可翻转）**不同族** ⇒ 那两个必须不过滤，这一个过滤**正确**。判据一句话：**守卫读封闭输入集时过滤正确，读全仓时过滤是蒙眼布** |
| C | `paths` 是 **workflow 级**（T34 已记）⇒ 被过滤的 job **不能**与必须保持不过滤的 `ci-scripts.yml` 同文件 ⇒ **新建独立 workflow**，且**不动任何既有 workflow 的 `paths`/`steps`** |
| D | 零依赖：守卫纯 stdlib（`re`/`pathlib`）⇒ job 内**无 `pip install`**，不会随依赖漂移。且它写 `-> str | None` / `list[str]`，靠 `from __future__ import annotations` 才在 3.9 上跑 ⇒ 3.9 矩阵腿把这条**隐性要求**钉住 |
| E | 本票**只接入、不重构**：`.agents/skills/**`(51) 与 `AGENTS.md`(1) 在模板登记面里，本地修好会被同步覆盖；`docs/agents/*.md`(4) 与 `docs/workflows/codex-issue-flow.md` **既不在模板登记、此前也无任何门禁** |

## 变更

1. **新增** `.github/workflows/ci-codex-flow.yml`（**6055 B**，115 CRLF，loneLF 0，含尾 CRLF）：单 job `wiring`、矩阵 `["3.9","3.12"]`、**3 个 step**、**无 `pip install`**；`paths` 9 条（push 与 pull_request **逐条相同**）。文件头逐段写清：守什么 / 为什么必须独立成文件 / 为什么这里能过滤而 `ci-scripts.yml` 不能 / 为什么 3.9 腿有意义 / 两方向消融读数 / 非目标与两处账。
2. `scripts/check-codex-issue-flow.py`：docstring 点名**谁运行它**（此前只写「不是什么」，读者无从知道它已被武装）。**3738 → 3984 B**，CRLF 102 → 107，行为逐字不变。
3. `docs/workflows/codex-issue-flow.md`：新增 `## Repo-local wiring` 小节（置于 `## Large initiatives` 之前）指向守卫。**2223 → 2755 B**，CRLF 37 → 46。可发现性面：不读 workflow 的人也能找到入口。
4. `tests/python/test_check_tasks_refs.py`：语料钉值 `(789,790)` → **`(790,791)`**（本票 `implement.jsonl` 自引 `prd.md`，是唯一的 `.trellis` 前缀引用 ⇒ +1）；**同票**修掉 docstring 首行已过期的自述（仍写 `as of T33 / #683: 788`，而其 T34 小节已写到 790 —— 同段落内自相矛盾）。**19140 → 19699 B**，462 → 470 行。

> 注：`paths` 9 条用**逐条精确路径**而非 `docs/**` 宽 glob —— 守卫断言的正是这 4+1 个文件，精确列出才能让「`paths` 就是输入集」这句话**可逐条核对**。删除文件也会命中旧路径，故不丢信号。

## 消融（两方向，缺一不算）

### 方向一 —— 守卫有牙（合成根，**真仓未被触碰**）

在 `tempfile` 合成根里搭出与真仓同形的结构（14 技能 + 6 文件 + `openai.yaml`），把守卫复制进去。基线在**合成根与真仓都为绿**（`rc 0`，`Codex issue workflow validation passed.`）。六处结构突变，**一次一处**：

| 突变 | 期望失败行 | 实测 |
|---|---|---|
| M1 `AGENTS.md` 丢 `$ultimatecoders-issue-flow` 指针 | `AGENTS.md does not point to …` | rc 1 ✓ |
| M2 某技能 `name:` 漂移（`tdd` → `tdd-renamed`） | `skill name mismatch: …` | rc 1 ✓ |
| M3 入口技能插入 `[TODO` | `entry skill contains an unfinished TODO` | rc 1 ✓ |
| M4 `agents/openai.yaml` 去掉 `default_prompt:` | `entry skill metadata is missing a default prompt` | rc 1 ✓ |
| M5 入口技能丢掉 `$code-review` 引用 | `entry skill is missing reference: $code-review` | rc 1 ✓ |
| M6 删除 `docs/agents/domain.md` | `missing workflow file: docs/agents/domain.md` | rc 1 ✓ |

**失败集合两两不相交**（每条突变打红**唯一**一条断言）⇒ 没有哪条突变是装饰，六个断言族各自独立被钉住（判据取自 `topics/t30-guard.md`：「两处突变打红同一集合 ⇒ 其中一条是装饰」）。

### 方向二 —— 旧形状对同一突变是瞎的（真仓，判词差）

真仓 `AGENTS.md` 做一处突变（替换指针），**CI 今天实际在跑的 5/5 命令判词全部不变（GREEN）**，只有新接入的 `python scripts/check-codex-issue-flow.py` **翻红**：

| 命令（既有） | M0 未改动 | M1 突变 | 判词 |
|---|---|---|---|
| `ruff check scripts/check-spec-refs.py tests/python/test_check_spec_refs.py` | GREEN | GREEN | 不变 |
| `ruff check scripts/`（T34 新增的目录级） | GREEN | GREEN | 不变 |
| `python scripts/check-spec-refs.py --audit` | GREEN | GREEN | 不变 |
| `python scripts/check-tasks-refs.py --audit` | GREEN | GREEN | 不变 |
| `python scripts/check-journal-ledger.py --verbose` | GREEN | GREEN | 不变 |
| **`python scripts/check-codex-issue-flow.py`（本票接入）** | GREEN | **RED** | **翻转** |

只证「新命令会红」**不够**（T19 判据）—— 必须让旧形状在**同一突变**下**保持绿**，才排除「只是重复旧覆盖、其实什么都没加」。

**恢复口径（按字节 + 独立认证）**：`AGENTS.md` 的 `git ls-files --eol` 报 `i/lf w/crlf` ⇒ **blob 是 LF、工作树是 CRLF**，所以「与 blob 逐字节相等」**不是**正确的恢复判据；改为**快照工作树字节 → 恢复该快照**，并由三重独立证据认证：① **另一进程**从磁盘复算 sha256 = 快照值 `db9939a3e77770f6…`；② `git diff --exit-code HEAD -- AGENTS.md` `rc 0`；③ `git status --porcelain` 为空。

> ⚠️ 这是 T34 记下的同类坑的**镜像**：T34 是「blob 是 CRLF、工作树是 LF」，这里是「工作树 CRLF、blob LF」—— **同一源（`core.autocrlf=true`），方向相反**。任何「与 blob 比对」的判据都必须**先问行尾方向**。

## 门禁与 CI

**本地**（`.venv/Scripts/python.exe`，ruff 0.16.3）：

- `python scripts/check-codex-issue-flow.py` → `rc 0`，`Codex issue workflow validation passed.`（3.13 与本机 venv 各一次）
- `ruff check scripts/` → `All checks passed!`（T34 的门禁未被本票打红）
- `check-spec-refs.py --audit` → `rc 0`；`check-tasks-refs.py --audit` → `rc 0`，**`791 ok / 0 dangling / 0 malformed`**；`check-journal-ledger.py` → `rc 0`
- `test_check_tasks_refs.py` → **14 passed**；Python 总收集 **1211 不变**
- `yaml.safe_load` 通过；job 数 **1**、step 数 **3**、`paths` 条目 **9**（push/pull_request 逐条相同）；**既有 workflow 零改动**（`git diff --stat` 只含本票 4 个文件）
- 被断言路径 **21**（逐条 vs `git ls-files` 全 tracked）

**CI（`fc7f6a3`，push 后实测）**：

| workflow | job | 结论 |
|---|---|---|
| **Codex Issue-Flow CI**（新） | `wiring (Python 3.9)` / `wiring (Python 3.12)` | **success** / **success** |
| Scripts CI | 5/5（`ruff lint (the whole scripts/ directory)`、`spec-refs` ×2、`tasks-refs` ×2） | **success** |
| Python CI | 4/4（`dashboard checks`、`test (3.9)`、`test (3.12)`、`ruff lint`） | **success** |

新 workflow 的 `run the wiring guard` 步骤在 **Python 3.9.25** 上逐字输出 `Codex issue workflow validation passed.`（`run id 35410506423`）—— 3.9 腿确实把 `from __future__ import annotations` 这条隐性要求钉住了。`test (Python 3.9)` 日志：`1203 passed, 8 skipped in 30.78s`（与基线逐字同）。

**语料钉值的两个可达态都被 CI 实测**：CI 上归档提交尚未推送 ⇒ `tasks-refs` 两个 Python 版本各报 **790 ok**；本地归档提交落盘后 ⇒ **791 ok**。两者都落在钉值 `(790,791)` 内。

## 提交

| Hash | Message |
|---|---|
| `fc7f6a3` | `ci(workflows): run the Codex issue-flow wiring guard in CI (#685)` — 4 files, +141/−4 |
| `f25acd7` | `chore(task): archive 09-19-t35-arm-codex-flow-guard` — 4 files, +183（含 `task.json`，归档提交按仓规带上） |

推送用 `-c http.proxy=` 单命令覆盖（本仓 `http.proxy` 指向**死端口**，直连会卡）；`git ls-remote origin main` 回读 = `fc7f6a3…` 背书。


### Git Commits

| Hash | Message |
|------|---------|
| `fc7f6a3` | `ci(workflows): run the Codex issue-flow wiring guard in CI (#685)` — 4 files, +141/−4 |
| `f25acd7` | `chore(task): archive 09-19-t35-arm-codex-flow-guard` — 4 files, +183（含 `task.json`） |

### Testing

- [OK] `python scripts/check-codex-issue-flow.py` → `rc 0`，逐字 `Codex issue workflow validation passed.`（本机 venv 与 3.13 各一次）
- [OK] `ruff check scripts/` → `All checks passed!`（T34 的门禁未被本票打红）
- [OK] `check-spec-refs.py --audit` `rc 0`；`check-tasks-refs.py --audit` `rc 0` + **`791 ok / 0 dangling / 0 malformed`**；`check-journal-ledger.py` `rc 0`
- [OK] `test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**（判回归只看总收集数对账）
- [OK] `yaml.safe_load` 通过；job **1** / step **3** / `paths` **9**（push 与 pull_request 逐条相同）；**既有 workflow 零改动**，`git diff --stat` 只含本票 4 个文件
- [OK] 被断言路径 **21/21 全部 tracked**（逐条 vs `git ls-files`）⇒ 接入无生来红风险
- [OK] 消融方向一（守卫有牙，**真仓未被触碰**）：合成根 6 处结构突变 M1–M6 **各自单独施加都 rc 1**，失败集合**两两不相交**；基线在合成根与真仓**都为绿**
- [OK] 消融方向二（**判据性**：旧形状对同一突变是瞎的）：真仓 `AGENTS.md` 一处突变下 **5/5 既有命令判词不变（GREEN）**，只有新命令 **RED** ⇒ 判词差成立，不是重复覆盖
- [OK] 突变按**工作树字节快照**恢复（`git ls-files --eol` 报 `i/lf w/crlf` ⇒ 「等于 blob」**不是**正确判据），由**另一进程**复算 sha256 认证；`git diff --exit-code HEAD -- AGENTS.md` `rc 0`、`git status` 空
- [OK] **CI（`fc7f6a3`）**：新 **Codex Issue-Flow CI 2/2 success**（`wiring (Python 3.9)` 日志逐字 `Codex issue workflow validation passed.`，运行时 3.9.25）；**Scripts CI 5/5 success**；**Python CI 4/4 success**（`test (3.9)` = `1203 passed, 8 skipped in 30.78s`，与基线逐字同）
- [OK] 语料钉值的两个可达态都被 CI 实测：CI 侧归档未推 ⇒ **790 ok**（3.9 与 3.12 各一次）；本地归档落盘 ⇒ **791 ok** ⇒ 均落在钉值 `(790,791)` 内

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`fc7f6a3` 实现 + `f25acd7` 归档 + 本会话的账本提交）；`fc7f6a3` 上 Codex Issue-Flow / Scripts / Python CI **全绿** ⇒ **#685 可关**（贴验收映射）。
- **本票立起的判据**：`paths` 过滤的**对错取决于输入集是封闭还是开放** —— 读**封闭输入集**（硬编码路径表、从不 glob 目录）时过滤**正确**；读全仓 / 读开放目录集时过滤是**蒙眼布**。本票是前者（21 条固定路径），`spec-refs` / `tasks-refs` 是后者（故必须不过滤）。
- **行为账（本会话实测踩到）**：`add_session.py --content-file` 指向**不存在的路径**时**静默**退回 `(Add details)`，无报错无警告（Git Bash 的 `/c/...` 传给 Windows Python 变成 `C:\c\...`）。唯一会红的是 `check-journal-ledger.py` 的占位符**整行相等**判据 ⇒ **写完账本必跑 ledger 守卫**，别信 `add_session` 自己打的 `[OK]`。
- **未决账（不静默丢弃）**：`ruff format --check` 仍未接线（T34 记）；`.trellis/.template-hashes.json` 被 `.trellis/scripts/common/safe_commit.py` **读取**但**无 job 校验其与工作树一致** —— 是否应由 CI 校验取决于上游模板意图（同步时机 / 是否允许本地改），**仓内无法坐实** ⇒ **记账待决，不臆造**；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 仍不在任何 ruff target 内（**已登记排除**，非欠账）。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。


## Session 43: T36 list the Rust inputs the Python test job compiles (#686)

**Date**: 2026-09-19
**Task**: T36 list the Rust inputs the Python test job compiles (#686)
**Branch**: `main`

### Summary

把 ci-python.yml 的 test job 真正编译的 Rust 依赖闭包（uc-types / uc-engine / uc-grpc + 工作区根）加进它的 paths —— 该 job 用 maturin 编译 crates/uc-python，但其 paths 一条 crates/** 都没有，历史语料显示 43/218 的推送因此让 Python 侧对引擎的检查静默不跑。不加 crates/**（uc-grpc-server 不在闭包内）。同票按推论 A 重写两版 README 过期的 CI 段（原文写 2 套 workflow，实为 8 套）。

### Main Changes

接 T35 / #685 立起的规则「门禁依赖的输入文件必须全列进 `paths`」，本票是把它用在一个**既有** job 上 —— 不是新立判据，而是拿既有判据去量一个此前没量过的面。

缺口：`ci-python.yml` 的 `test` job 用 `maturin develop --release --manifest-path crates/uc-python/Cargo.toml` **编译 Rust**，再跑 `pytest tests/python/`；而它的 `paths`（`python/**`、`tests/**`、`pyproject.toml`、`dashboard/**`、自身）**一条 `crates/**` 都没有**。⇒ 只改 `crates/**` 的推送会让 Python 侧对引擎的检查**静默不跑**。

## 起点一手复核（不继承上一轮叙述）

| 检查 | 命令 / 依据 | 读数 |
|---|---|---|
| 日期 | `date` | `Sat Sep 19 08:35` |
| HEAD | `git rev-parse HEAD` | `de35f0b` |
| 远端 | `git ls-remote origin main` | `de35f0b`（回读，与本地相等） |
| 工作树 | `git status --porcelain` | 空 |
| 开放 issue | `gh issue list --state open` | **1**（#656，3 条评论，末次更新 `2026-09-18T13:45Z`，**无新内容**） |
| 归档最大票号 | `ls .trellis/tasks/archive/2026-09/` | 最大 `-t35-` ⇒ 本票 **T36** |
| issue 最大号 | `gh issue list --state all` | 685 ⇒ 本票 **#686** |

⇒ 唯一可推进的仍是**框架卫生线**，且**不依赖**外部「方案第 21 节」。

## 缺口的一手读数（HEAD `de35f0b`）

| 检查 | 命令 / 出处 | 读数 |
|---|---|---|
| 该 job 是否编译 Rust | `ci-python.yml:68-73` | `maturin develop --release --manifest-path crates/uc-python/Cargo.toml` |
| `paths` 是否含 crates | `ci-python.yml:7-20`（`push` + `pull_request`） | **0 条** `crates/**` |
| 该 `paths` 是否**曾**含 crates | `git log -p -- .github/workflows/ci-python.yml \| grep '^[+-].*crates'` | 唯一命中是**那一步**（`maturin … crates/uc-python/Cargo.toml`），**没有一条 `paths` 条目** ⇒ **从未列过** |
| 闭包推导 | `crates/uc-python/Cargo.toml` `[dependencies]` | `uc-types`、`uc-engine`（`default-features=false, features=["storage","indexing"]`）、`uc-grpc` |
| 反向确认 | 同文件：**无** `uc-grpc-server` | 独立 binary crate ⇒ 不在闭包内 |
| 其他根级构建配置 | `git ls-files 'rust-toolchain*' '.cargo/**' 'clippy.toml' 'rustfmt.toml' '**/build.rs'` | 只有 `crates/uc-grpc/build.rs`（**在** `crates/uc-grpc/**` 内）⇒ 闭包无遗漏项 |
| Python 侧是否真消费 | `python/ultimate_coders/engine.py:17` | `from ultimate_coders._uc_core import PyEngine, PySearchQuery`（`try/except ImportError` 兜底 `None`） |
| 谁在测它 | `git grep -ln 'ultimate_coders.engine\|PyEngine\|_uc_core' -- tests` | **2** 个文件：`test_async_engine.py`（25 条，本地 **25 passed in 0.61s**，**不在** 8 个 skipped 里）、`test_affinity_placement.py` |
| 暴露面大小 | `git grep -c '#\[pyclass\]\|#\[pymethods\]' -- crates/uc-python` | `engine.rs` / `scheduler.rs` / `types.rs` + `lib.rs` 的 `#[pymodule]`，合计 **30+ 处**装饰器 |

## 历史语料消融（可复算；语料与索引同源）

1. `gh api "repos/…/actions/runs?branch=main&per_page=100&page=N"`（N=1,2,3）⇒ **300 runs / 219 push heads**（**218** 条可用改动集）。
2. push 改动集 = `git diff --name-only <上一个 push head>..<本 push head>`；非祖先对 **0**。
3. **地面真值** = API 里该 head 是否存在 `Python CI` run，与改动集计算**无关**。
4. 匹配器把 `paths` 的 glob 逐条对上改动集（`a/**` 视作前缀 `a/`；非 glob 条目精确相等）。

| 方向 | 读数 | 判读 |
|---|---|---|
| **模拟器自校（关键对照）** | 现行 `paths` 命中 **65/218**；API 显示 Python CI 跑了 **65/218** | **逐行一致** ⇒ 匹配器复刻了 GitHub 的判词，故它对**新** `paths` 的预测可信 |
| **A 不丢覆盖** | Python CI 跑过的 65 条，新 `paths` **全部仍命中**（违例 **0**） | 只加不删 |
| **B 本票要修的洞** | 改了闭包而旧 `paths` 命中 0 的推送 = **43**；其中 Python CI **真跑了 0 条**；新 `paths` **命中 43/43** | 「改动集 ⊄ 触发面」= **43/218 ≈ 20%** |
| **C 不过度触发** | 语料内「只动闭包外 crate」的推送 = **0** ⇒ 该方向**在语料上为空**（如实记录，不谎报成已验证） | 改用**结构性断言**：`crates/uc-engine/src/x.rs` ✅ / `crates/uc-python/src/lib.rs` ✅ / `Cargo.lock` ✅ / `crates/uc-grpc-server/src/x.rs` ❌ / `crates/uc-grpc-server/Cargo.toml` ❌ ⇒ **5/5 符合预期** |

**单提交直证**：`gh api ".../actions/runs?head_sha=1e613184…"` ⇒ `total_count = 1`，唯一 run = **Rust CI**（那次只改了 `crates/uc-engine/**`，Python CI 静默）。

## 结论：加**推导出的闭包**，不加 `crates/**`

| # | 结论 |
|---|---|
| A | 触发面必须**等于真正输入集**；该 job 的输入集是 `Cargo.toml` 推导出的闭包，不是整个工作区 |
| B | 闭包 = **4 个 crate + 工作区根**（`Cargo.toml` / `Cargo.lock`）⇒ 精确 6 条 |
| C | **`uc-grpc-server` 不在闭包内**（独立 binary）⇒ 加 `crates/**` 是**过度触发**（语料里有 2 笔只动它的提交） |
| D | 只加 `crates/uc-python/**` **不够**：`uc-engine` 的语义变化会改变 extension 行为而不改 `uc-python` 一行 —— 那正是 T33「相邻两层各测各的 ⇒ 接缝无人守」的形状 |
| E | 不新开「seam」workflow：`paths` 是 workflow 级 ⇒ 新 workflow 需自列 `crates/**` + `python/**` + `tests/**` + 自身（否则造出**新的**盲区），而省下的只是 `pytest` 那 ~31s —— **成本大头在 `maturin` 构建与 setup，两者都省不掉** |
| F | 这**不是**已决冻结：T26 的「三选项」是关于 **`scripts/**`** 的裁决（`scripts/**` 不是该 job 的输入 ⇒ 选 C 正确）；触发面表（09-16）把两个触发集写成互不相交，**没有**把它记成欠账 |
| G | 严重度是「中」不是「致命」：只有 2 个测试文件引用 extension，且 `engine.py:17` 的 import 在 `try/except ImportError` 里 ⇒ **按「输入集 = 触发面」修，不夸大成语义缺陷** |
| H | **推论 A 同票修**：两版 README 把 Python CI 的触发面写成 `python/`、`tests/`、`pyproject.toml`，改完 `paths` 后该陈述**变为假** |
| I | 顺带实测发现 README 的整个 CI 段**早就过期**：段首写「**Two** independent CI workflows」而仓内实有 **8** 套、触发是「**推送到 `main` 与面向 `main` 的 PR**」（原文只说 PR —— 而**推送**正是 T36 缺口隐形的原因）⇒ **整段重写**，不是只改两格 |

## 变更

1. `.github/workflows/ci-python.yml`（**4374 → 6912 B**，CRLF 152，loneLF 0）：`push` 与 `pull_request` 各由 5 条 `paths` 增至 **11 条**（`crates/uc-python/**`、`crates/uc-types/**`、`crates/uc-engine/**`、`crates/uc-grpc/**`、`Cargo.toml`、`Cargo.lock`），两处**逐字相同**；文件头写清闭包推导、65/65 与 43/218 的读数、**为什么不是 `crates/**`**、以及维护规则（`uc-python` 新增依赖 ⇒ `paths` 必须**同 change** 更新）。**job 3 个 / step 17 个与改前逐字相同**。
2. `tests/python/test_check_tasks_refs.py`（**19699 → 20109 B**）：语料钉值 `(790,791)` → **`(791,792)`**；docstring 首行 `as of T35 / #685: 790` → `as of T36 / #686: 791`；新增 T36 段。
3. `README.md`（**26017 → 27704 B**，CRLF 479 → 485）与 `README.zh-CN.md`（**22995 → 24722 B**，CRLF 441 → 447）：CI 段由「2 套工作流」重写为**实有 8 套**的完整表格，每行带 workflow 文件名与真实触发路径；并加一句把**工作流文件本身**指为权威来源。

## 消融 —— 对账脚本先红后绿，再五轴突变

写 README 表格时我**没有**从记忆里抄路径，而是写了个「README 表 ↔ workflow YAML」对账脚本。它**当场抓出我自己的错误**：我为 `ci-codex-flow.yml` 写了 `` `docs/agents/*.md` `` —— 该文件里**没有这个 glob**，是 4 个具名文件。修正后转绿。

随后在**沙箱副本**上做 5 处单点突变（真仓未被触碰，每处按字节恢复）：

| 突变 | 轴 | 实测 |
|---|---|---|
| **A** 行内加假路径 `crates/uc-nope/**` | 声称 ⊄ 真值 | rc 1 ✓ |
| **B** 行内删真路径 `crates/uc-grpc/**` | 真值 ⊄ 声称 | rc 1 ✓ |
| **C** 表里改名不存在的 `ci-journalX.yml` | workflow 存在性 | rc 1 ✓ |
| **D** 只改 YAML（`ci-dashboard.yml` 加 `newdir/**`）不改 README | **真实漂移场景** | rc 1 ✓ |
| **E** 只改 `push` 不改 `pull_request` | 一格不能描述两者 | rc 1 ✓ |

**5/5 变红，且没有两条共享同一判词** ⇒ 该脚本的每条分支都被钉住，不是装饰。⚠️ B 与 D 打的是**同一条**分支（省略判据的两个漂移方向）—— 这本身没问题，但正因如此我补了 E，否则「`push != pull_request`」那条分支**没有任何突变到达**。

## 账（未静默丢弃）

- **账 1（本票新增）**：`paths` 与 `Cargo.toml` 的一致性**没有守卫**。是否值得写「从 `cargo metadata` 推导闭包并与 `paths` 对账」的守卫：**待决** —— 需 CI 有 Rust 工具链，且对账口径（是否含 dev/build-dependencies）需先裁，**仓内无法坐实**。
- **账 2（本票新增）**：**README CI 段与 YAML 之间没有守卫**。本票为满足推论 A 把该段重写成「8 行 × 全路径」，等于新造了一处**会漂移的重复**（它刚漂移了 6 套 workflow 没人发现）。对账脚本已完成并通过 5 轴自检，但**留在 `.workbuddy/tmp/`（被 ignore）、未入库**。口径已探明：它读**封闭输入集**（写死的两份 README + `.github/workflows/*.yml`）⇒ 若做成 job，`paths` 过滤对它**是正确的**；但 `ci-scripts.yml` 无过滤且其 job 必须保持无过滤 ⇒ 该 job 只能放进那里并同样无过滤（守卫廉价，可接受）。**是否升格为正式守卫：记账待裁。**
- **账 3（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。


### Git Commits

| Hash | Message |
|------|---------|
| `130c343` | `ci(python): list the Rust inputs the Python test job compiles (#686)` — 2 files, +56/−4（`ci-python.yml` +46，`test_check_tasks_refs.py` +14/−4） |
| `79543c4` | `docs(readme): correct the CI trigger table to the eight real workflows (#686)` — 2 files, +22/−10 |
| `dc3545a` | `chore(task): archive 09-19-t36-arm-python-test-inputs` — 4 files, +158（含 `task.json`，归档提交按仓规带上） |

### Testing

- [OK] `ci-python.yml` 通过 `yaml.safe_load`；`paths` **11** 条，`push` 与 `pull_request` **逐条相同**；job **3** / step **17** 与改前**逐字相同**（只动 `paths`）
- [OK] 四个守卫均 `rc 0`：`check-codex-issue-flow` / `check-spec-refs` / `check-tasks-refs` / `check-journal-ledger`
- [OK] `ruff check scripts/` 与 `ruff check python/ tests/` 均 `All checks passed!`
- [OK] 语料钉值 `(791,792)` 的**两个可达态都实测到**：任务目录未跟踪 ⇒ **791 ok / 0 dangling / 0 malformed**；归档落盘后 ⇒ **792 ok / 0 dangling / 0 malformed**
- [OK] `test_check_tasks_refs.py` **14 passed**；Python 总收集 **1211 不变**（判回归只看总收集数对账）
- [OK] 闭包推导可复算：`crates/uc-python/Cargo.toml` 的 `[dependencies]` = `uc-types` + `uc-engine(storage,indexing)` + `uc-grpc`；**无** `uc-grpc-server`；`git ls-files` 确认无其他根级构建配置遗漏
- [OK] 历史语料消融（300 runs / 218 可用推送）：模拟器自校 **65 = 65**（与 API 判词逐行一致）；A 违例 **0**；B **43/43**（旧 `paths` 命中 0，其中 Python CI 真跑 **0**）；C 结构性 **5/5**
- [OK] 单提交直证：`1e61318`（只改 `crates/uc-engine/**`）API `total_count = 1`，唯一 run = **Rust CI** ⇒ Python CI 当年确实静默
- [OK] **README ↔ YAML 对账（两版各一次）`rc 0`**：8 行工作流表格，每行声称的路径集 == 该 YAML 的 `paths` 去掉自身文件名
- [OK] 该对账脚本**先红后绿**：初稿为 `ci-codex-flow.yml` 写了 `` `docs/agents/*.md` ``，而 YAML 里是 4 个具名文件、**没有这个 glob** ⇒ 当场变红，修正后转绿
- [OK] **五轴单点突变 5/5 变红且无两条共享同一判词**（A 行内加假路径 / B 行内删真路径 / C 表里改名不存在的 workflow / D 只改 YAML 不改 README / E 只改 `push` 不改 `pull_request`），全在**沙箱副本**上做、真仓未触碰、每处按字节恢复
- [OK] **CI（推送 `79543c4`）**：8 套 workflow 中**恰好 2 套**触发 —— **Scripts CI**（无 `paths` 过滤）与 **Python CI**（自身 YAML 在 `paths` 内）⇒ 这同时是这份触发面表的**实测背书**；判词 **Scripts CI 5/5 success**（`ruff lint (the whole scripts/ directory)` + 两个守卫 × 3.9/3.12）、**Python CI 4/4 success**
- [OK] CI 逐字读数：Python 两腿均 **`1203 passed, 8 skipped`**（与基线一致）；tasks-refs job 内 **`791 ok / 0 dangling / 0 malformed`** + `SELF-CHECK PASSED: 6 mutations, all distinct`；pin 测试 **14 passed**

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`130c343` 实现 + `79543c4` 文档 + `dc3545a` 归档 + 本会话的账本提交）；`79543c4` 上 Scripts CI 与 Python CI **全绿** ⇒ **#686 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：触发面的正确形状 = **该 job 真正读到的输入集**。判据不是「宁多勿少」，而是**推导**出来的闭包 —— 用 `crates/**` 会过度触发（`uc-grpc-server` 不在闭包内）。⚠️ 闭包是从 `Cargo.toml` 推的 ⇒ **新增依赖必须同 change 更新 `paths`**（文件头已写这条维护规则）。
- **账 3（待裁）**：README CI 段与 YAML 之间**没有守卫**，而本票刚把该段重写成「8 行 × 全路径」= 新造一处会漂移的重复（它此前已漂移 6 套 workflow 无人发现）。对账脚本已完成、通过 5 轴突变自检，但**未入库**。口径已探明（读封闭输入集 ⇒ 过滤正确；只能放进无过滤的 `ci-scripts.yml`）。**是否升格为正式守卫：记账待裁。**
- **账 2（待决）**：`paths` 与 `Cargo.toml` 的一致性无守卫；写「`cargo metadata` 推导闭包并对账」需 CI 有 Rust 工具链 + 先裁对账口径（是否含 dev/build-dependencies），**仓内无法坐实**。
- **账 1（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。


## Session 44: T37 guard the README CI trigger table against the workflow YAML (#687)

**Date**: 2026-09-19
**Task**: T37 guard the README CI trigger table against the workflow YAML (#687)
**Branch**: `main`

### Summary

把 README 的 CI 触发面表（on.push.paths 的手抄副本，零守卫）升级为正式守卫并接进 CI：新增 scripts/check-readme-ci-table.py（7 条判据）+ ci-readme-ci-table.yml（3.9/3.12）+ 测试（8 处突变覆盖 7 条判据）。含一次真实漂移（新建第 9 套 workflow 必让表少一行）。

### Main Changes

## 缺口的一手读数（HEAD `4a5f1cc`）

| 检查 | 命令 / 出处 | 读数 |
|---|---|---|
| 该表存在 | 两版 README § CI | 各 **8 行**，每行 = 一个 workflow + 其触发路径 |
| 有没有人校验它 | `git grep -ln "README" -- scripts tests .github` | **0 处** —— 没有任何脚本或 workflow 读它 |
| 它会不会自己说话 | —— | **不会**：改 `paths` 不打红任何东西；改 README 也不打红任何东西 |
| 漂移史 | T36 / #686 重写时实测 | 该段此前漂称「**2 套**」而实有 **8 套**，**无人发现** |

⇒ 一处**会漂移的重复 + 零守卫**，而它的职责是告诉读者「改什么会触发什么」。**错在这张表上 = 把触发面的知识污染给每一个人**（T36 里我正是照着错表去推「Rust CI 会跑」的）。

**这个缺口不是推演出来的，是上一票撞出来的。** T36 重写该表时，我为 `ci-codex-flow.yml` 写了 `` `docs/agents/*.md` `` —— 该 YAML 里是 **4 个具名文件**、**没有这个 glob**。它格式正确、语气一致、看起来完全合理；**只靠阅读绝不会发现**。抓到我的是一个临时对账脚本，而它留在被 ignore 的目录里，**抓到一次就作废了**。本票就是把它升格为正式守卫。

## 结论与取舍（逐条写明为什么不选另一条）

| # | 结论 / 取舍 |
|---|---|
| A | 该表是 `on.push.paths` 的**手抄副本**，而「手抄」这一步**没有也不会**变可靠 ⇒ **必须**由机器对账，不能靠人读 |
| B | 守卫的输入集是**封闭**的（写死的两份 README + `.github/workflows/` 这**一个**目录）⇒ 按 T35 / #685 判据，`paths` 过滤**是正确的** |
| C | `paths` 是 **workflow 级** ⇒ 带过滤的 job **不能**放进必须保持无过滤的 `ci-scripts.yml` ⇒ **新建独立 workflow**，不动任何既有 `paths`/`steps` |
| D | **PyYAML，不手写 YAML 子集解析器**：判据是「**永远不会红的守卫不是证据**」—— 手写解析器在缩进/引号/内联列表变化时**静默返回空**，守卫变**假绿**，这是本票**最不能**犯的错 |
| E | 但 README 那一半**只能**手写解析（表格是给人读的）⇒ 用**非空性断言**防住「解析静默返回空」，这**比选哪个解析器更要紧** |
| F | 本票**自带一次真实漂移**：新建第 9 套 workflow 后表格**必然**少一行 ⇒ 守卫**必然先红**。**这不是合成突变，是真漂移**，比任何沙箱消融都强 |
| G | 顺带钉住另一条散文陈述：README 说各 workflow「都指向 `main`」⇒ 守卫一并校验 `branches == ["main"]`（同一份 YAML 读，不新增输入面） |

七条判据（**各带自己的失败消息**，便于逐条消融钉住）：非空性 / 存在性 / 对称性 / 不虚构 / 不省略 / 过滤形状 / `branches == [main]`。判据 4/5 的对账口径：**声称集 == 该 YAML 的 `paths` 去掉自身 YAML 文件名**（自身文件由 README 的一句脚注统一说明，不占表格格子）。

## 消融（两路，缺一不算）

| 方向 | 做法 | 读数 |
|---|---|---|
| **一 真实漂移** | 新增第 9 套 workflow（本票自己）⇒ 表**必然**少一行 | 两版 README 各报一条 `workflows on disk but not in table: ['ci-readme-ci-table.yml']` ⇒ `rc 1`；补第 9 行 + 段首 `Eight`→`Nine` + 脚注重写 ⇒ `rc 0` |
| **二 八处合成突变** | 沙箱副本，逐处单独施加并按字节恢复 | **8/8 变红**，且**七条判据每条至少被一处单独打到** |

八处：A 行内加假路径 / B 行内删真路径 / C 表里改名不存在的 workflow / D 只改 YAML 不改 README / E 只改 `push` 不改 `pull_request` / F `branches` 改离 `main` / G 删掉某 workflow 的 `paths` 过滤块 / H 把 `## CI` 标题改名。

⚠️ **两条方法论（T36 记下的，本票照做且被证明必要）**：
1. **「每条分支都被钉住」>「突变数够多」** —— 第 6 条（过滤形状）与第 7 条（`branches`）在 A–D 里**根本走不到**；C 与 D 又都打在**同一条**省略判据上 ⇒ **必须**补 G 与 H，否则它们**没有任何突变到达**而照样有代码。测试因此**断言判据清单被覆盖**，而不是只看「rc 非零」。
2. **一次只动一个轴** —— D **同时**改 `push` 与 `pull_request`，否则打红的是**对称性**而不是**省略**。

## 两处锚点事故（不修就会让突变被静默吞掉）

| 事故 | 症状 | 修法 |
|---|---|---|
| **E 的锚点落在散文里** | 断言是 `0 != 0` —— 突变**什么都没改**，守卫照绿 | `ci-trellis.yml` 的文件头注释里**第一处** `.trellis/scripts/**` 出现在**散文**中 ⇒ 第一次出现替换改的是注释。改为锚定 `      - "{path}"`（带缩进与引号）**并断言期望出现次数为 2**，锚点写错就**大声失败** |
| **G 的多行锚点在 CRLF 文件里用 `\n`** | 锚点出现 **0 次**（期望 2） | `mutate()` 里多行锚点改用**该文件自己的行尾**；否则锚点匹配零次，突变**静默失效** |

⇒ 八处突变**每一处的锚点都声明期望出现次数**，从「静默什么都不做」变成「大声失败」。这条是 T37 自己付的学费。

## 账（未静默丢弃）

- **账 1（延续 T36）**：`paths` 与 `Cargo.toml` 的一致性**仍无守卫**（需 CI 有 Rust 工具链 + 先裁对账口径，**仓内无法坐实**）。
- **账 2（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- **账 3（本票新增）**：本守卫只覆盖「**触发路径**」这一张表。README 里**其他**手抄的机器可读面（测试基线数、job 数等）**仍无守卫** —— 是否扩面：**待决**（先把这一张钉死，别一次铺太宽）。

### Git Commits

| Hash | Message |
|------|---------|
| `940dfb4` | `ci(workflows): guard the README CI trigger table against the workflow YAML (#687)` — 6 files, +577/−8（守卫 213 / 新 workflow 112 / 测试 236 / 两版 README 各 +5−2 / 语料钉值 14） |
| `e4bde60` | `chore(task): archive 09-19-t37-guard-readme-ci-table` — 4 files, +173（含 `task.json`，归档提交按仓规带上） |

### Testing

- [OK] 新守卫对当前真仓 `rc 0`，报告 **`9 workflow(s) reconciled in 2 file(s)`**
- [OK] **真实漂移，先红后绿**（本票独有，非合成）：新建第 9 套 workflow 后，两版 README 各报一条 `workflows on disk but not in table: ['ci-readme-ci-table.yml']`；补第 9 行 + 段首 `Eight`→`Nine` + 脚注重写后转绿
- [OK] **八处合成突变 8/8 变红**，且**七条判据每条至少被一处突变单独打到**（由 `test_every_judgment_is_pinned_by_a_mutation` 断言强制，不是靠人数出来的）：A 行内加假路径 / B 行内删真路径 / C 表里改名不存在的 workflow / D 只改 YAML 不改 README / E 只改 `push` 不改 `pull_request` / F `branches` 改离 `main` / G 删掉某 workflow 的 `paths` 过滤块 / H 把 `## CI` 标题改名
- [OK] 突变全在**沙箱副本**上做、真仓未触碰、每处按字节恢复；`test_mutations_are_independent` 单独证明「恢复真的是恢复」（否则第 N+1 处测的是第 N 处）
- [OK] 四个既有守卫均 `rc 0`：`check-codex-issue-flow` / `check-spec-refs` / `check-tasks-refs` / `check-journal-ledger`
- [OK] `ruff check scripts/` 与 `ruff check python/ tests/` 均 `All checks passed!`；⚠️ 首轮实测出 **2 处真实 E501**（判据 3 与判据 6 的失败消息超 100 列），已按隐式拼接改写且**输出字符串逐字不变**
- [OK] 语料钉值 `(792,793)` 的**两个可达态都实测到**：任务目录未跟踪 ⇒ **792 ok / 0 dangling / 0 malformed**；归档落盘后 ⇒ **793 ok / 0 dangling / 0 malformed**
- [OK] `tests/python/test_check_tasks_refs.py` **14 passed**；Python 总收集数 **1211 → 1214**（= 本票新增 3 例，逐字吻合）
- [OK] **CI（推送 `940dfb4`）**：9 套 workflow 中**恰好 3 套**触发 —— 新 workflow、**Scripts CI**（无 `paths` 过滤，**5/5 success**）、**Python CI**（`tests/python/**` 命中，**4/4 success**）⇒ 这同时是 README 第 9 行那条**新陈述的实测背书**
- [OK] CI 逐字读数：新 workflow **两腿（3.9 + 3.12）均 success**、均报告 `9 workflow(s) reconciled in 2 file(s)` 且各自 **`3 passed`**（3.9 腿在跑 ⇒ `set[str] | None` 所依赖的 `from __future__ import annotations` 前提**仍成立**）；Python 两腿均 **`1206 passed, 8 skipped`**（基线 1203 + 本票 3 例）；tasks-refs job 内 `792 ok / 0 dangling / 0 malformed` + `SELF-CHECK PASSED: 6 mutations, all distinct`

### Status

[OK] **Completed**

### Next Steps

- 已直落 main（`940dfb4` 实现 + `e4bde60` 归档 + 本会话的账本提交）；`940dfb4` 上三套 CI **全绿** ⇒ **#687 可关**（贴验收映射）。
- **本票立起的判据（可复用到下一条）**：**手抄的机器可读面必须有守卫**。判据的落点不是「这张表对不对」，而是「**它还会不会再漂**」—— T36 已经写了一句「以 YAML 为准」的**劝告**，**仍然**是我自己写错的 ⇒ **一句话劝告不是判据**（本票据此把「只加一句说明」这个方案明确否决）。
- **本票付的学费（写进技能）**：① 「**每条分支都被钉住**」>「突变数够多」；② **一次只动一个轴**；③ **锚点必须声明期望出现次数**，否则注释/散文会吸收突变而守卫照绿；④ 手写解析器（README 表格这一半）**必须**配非空性断言。
- **账 1（延续 T36）**：`paths` ↔ `Cargo.toml` 一致性**仍无守卫**（需 CI 有 Rust 工具链 + 先裁对账口径，仓内无法坐实）。
- **账 2（延续）**：`ruff format --check` 仍未接线；`.trellis/.template-hashes.json` 无 job 校验；`.trellis/scripts/**`(27) 与 `.claude/hooks/**`(3) 按登记排除。
- **账 3（本票新增）**：README 里**其他**手抄的机器可读面（测试基线数、job 数等）**仍无守卫** —— 是否扩面：**待决**（先把这一张钉死，别一次铺太宽）。
- #656（P2 本体）仍只等外部「方案第 21 节」原文。
