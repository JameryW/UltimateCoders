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
