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
