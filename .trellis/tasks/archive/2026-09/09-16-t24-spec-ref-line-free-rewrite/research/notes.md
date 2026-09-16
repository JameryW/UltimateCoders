# T24 侦察与证据（#673 切片 B）

所有数字都是**跑出来的**；每条都给出复算命令。`.scratch/` 脚本不入库（`.gitignore:135`）。

## §A 工作清单：谁可以去行号，为什么

分类是**优先级阶梯**（与 T22 的「仅符号 / 仅内容 / 两者」**不是同一套划分**，别把两套数字相比）：

```python
if row["symbol"] is not None:                     return "symbol"           # 42
if row["content"] and occurrences == 1:           return "content-unique"   # 9
if row["content"]:                                return "content-ambiguous"# 32
if row["content_candidate_count"] > 0:            return "content-mismatch" # 0
return "no-anchor"                                                          # 65
```

⇒ 合格 **51**（42 + 9），扣下 **97**（32 + 65）。`no-anchor` 与 `content-mismatch` **互斥**：
`content_candidate_count > 0` 表示「本行确实引了代码但目标文件里没有」，
`no-anchor` 表示「本行没有任何可用锚」。实测 **0 条** `content-mismatch` ——
T22 记录的那 4 条是 `step_condition.py` 语法表的元变量（`!expr` / `a && b`），
按构造豁免（不含 `_` `=` `.` `"`）⇒ 落入 `no-anchor`，与记录一致。

**唯一性**是 T23 加的、也是本票的判据：41 条内容锚里 **32** 条的匹配字面量在目标文件出现 **>1** 次
（`parallel_group` **20** / `retry_count` **15** / `abort_on_failure` **14** / `agent_config_json` 12 /
`retry_delay_ms` 9）。去掉行号后它们无法区分「指的是哪一处」⇒ 不去。

## §B 干跑为什么不能用影子树（一次被实测推翻的设计）

首版 `t24-rewrite.py` 把 `.trellis/spec` 拷进临时树并把 `ROOT` 一起指过去，得到
**216/221 目标解析不到**。原因是守卫的解析依赖**真仓库**：

- `_repo_index()` 走 `ROOT` 建 basename → 路径的索引；
- `_line_count(rel)` 读 `ROOT/rel`；
- `collect()` 里 `spec.relative_to(ROOT)` —— 影子树在仓外会直接 **ValueError**，在仓内则把自己
  的 `.md` 加进索引（改变 bare basename 的歧义性）。

⇒ 改为：干跑只预测**形态**；**写盘后**跑真守卫，失败按字节回滚。
回滚源放在**仓外**（`%TEMP%/t24-backup`），理由见 §D。

## §C 形状闸门，与唯一被扣下的那条

提及扫描只读**反引号 span**、且**围栏之外**。于是「去行号后守卫还看得见吗」是**可判定**的：

```python
for start, end, span in _tick_spans(line):
    if start <= col < end:
        form = (span[:offset] + ref + tail[m.end():]).strip()   # 删掉 :行号
        return bool(MENTION_PATH_RE.fullmatch(form)), form
return False, "not inside backticks"
```

跑出来**恰好 1 条**不合格，且是同一行上的**两个**独立理由：

```
error-handling.md:320  sandbox.py  -> 'sandbox.py'
   not inside backticks (the mention scan reads backticked spans only)
```

原文（`repr`，逐字节读过）：

```
'`FileChange.diff` is commonly `""` (the worker\'s `_parse_agent_file_changes` sets
`diff=""` at sandbox.py:1007/1546 — it records the path + change_type but not a diff). …'
```

- 全文**唯一**的**裸写**引用（无反引号）⇒ 提及类按定义看不见它；
- **唯一**的 `/双行号` 形态 ⇒ 机械删除会产出 `sandbox.py/1546`，`MENTION_PATH_RE` 不匹配。

两个理由**都在**这条上，所以扣下它是唯一正确处置。要真正修它得重写那句散文（**非目标**）。

## §D 应用结果，以及三个数字为什么是 50 / 49 / 49

```
refs 148 -> 98   (planned 50 removals)
mentions 221 = 153 resolved / 47 dangling / 21 ambiguous
surviving refs: 98 -- every verdict unchanged
ZERO COVERAGE LOSS: each rewritten ref -> a resolved mention with the same target
of 50 rewritten, 41 still name a symbol defined in the target; 9 now rely on path resolution alone
guard exit=0
```

| 量 | 值 | 解释 |
|---|---|---|
| **重写发生次数** | **50** | 51 合格 − 1 扣下 |
| 消失的 `(spec, line, ref)` 键 | **49** | `agent-capability-spec.md:603` 一行两引用、**且两条 `ref` 同名** ⇒ 键塌成 1 |
| `git diff --numstat` | **49 / 49** | 同一原因：50 处落在 49 行上 |

⇒ 三个数字**同源**，不是三处不一致。`assert len(after) == len(rows) - len(plan)` 用的是
**行数**（列表长度），所以不会因塌键而误判。

**重写买到了什么（分解）**：

```
pre-rewrite verdict of what got rewritten:
    25  OK
    24  STALE          ← 键口径；实际发生 25（塌键的那对里有一 OK 一 STALE）
pre-rewrite STALE total: 26
survivors still STALE   : 1
surviving refs by verdict: {'OK': 89, 'AMBIGUOUS': 8, 'STALE': 1}
```

⇒ **26 条 STALE 有 25 条本身就是漂移过的行号**（`114 ok/26 stale` → `89 ok/1 stale`，差 25 各就各位）。
这就是本票的实际收益：漂移的行号从「看起来权威」变成「没有行号」。
`agent-capability-spec.md:603` 那对之所以一个 STALE 一个 OK：同行的 `_execute_steps` / `_run_single_step`
对 `start=1053` 与 `start=1246` 的最近距离不同。

**幸存者**：98 = **89 ok + 1 stale + 8 ambiguous**；`index.md` 的 1 条引用被重写后该文件**剩 0 条**，
所以「有引用的 spec 文件」从 **12 → 11**（守卫首行输出即 11，不是笔误）。

## §E 消融：`.scratch` 子句（T0 / T1 / T2）

```
T0 committed   sha=7f4d983b91de8f03 exit=0
     221 line-free path mentions in 26 spec files (153 resolved / 47 dangling / 21 ambiguous)
     summary: 89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural failure(s)

T1 mutated     (.scratch removed) exit=0
     221 line-free path mentions in 26 spec files (152 resolved / 47 dangling / 22 ambiguous)
     summary: 89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural failure(s)

T2 restored    sha=7f4d983b91de8f03 exit=0  (byte-exact)
ablation PASSED
```

判据：T1 ≠ T0（子句**在起作用**）、T2 == T0（恢复**按字节**）、**三次 exit 全 0**
（新子句**没有**松动门禁）。refs 三项在 T0/T1 **完全相同** ⇒ 该子句**只**触及 advisory 面。

⚠️ 突变**从文件字节推导**，不写死字面量 —— 首版把 `EXCLUDE_DIRS` 两行按 `\n` 拼成字面量，
而该文件是 **CRLF**，于是断言 `count(...) == 1` 当场炸（**写盘前**炸，符合「assert 先炸再写盘」）。

**副作用（正向）**：排除 `.scratch` 之后，「仓内影子树 + 真 ROOT」这个**此前不可用**的测量手法
变得可用 —— §D 的 pre-rewrite 分解就是用它（`git show HEAD:` 重建 31 个 spec 文件到
`.scratch/t24-preview`）跑出来的。T23 正是因为缺这一步而只能放弃影子树。

## §F 修正与自纠（三处）

1. **52 → 51。** T23 记录的合格数包含 `error-handling.md:307`，它的锚是散文里的 `delete`。
   守卫收紧后该行零锚 ⇒ 合格数 51。已回写 `#673` 与本目录 `prd.md`；`topics/spec-ref-hygiene.md` 同步。
2. **`MemoryWriteError` 被我读错。** 我断言「它含 `_`，因此 `CONTENT_TOKEN_RE` 应接受它，
   守卫 `cand=0` 是缺陷」。逐条件打印后：`_` **不在**该标识符里（CamelCase），
   `search('_', 'MemoryWriteError') == False`、`search('_', 'A_B') == True` ⇒ **守卫是对的，我错了**。
   同一行里路径 span 被排除是**设计**（`PATH_SPAN_RE`），不是漏检。
   ⇒ **又一次「从字面量推断而非读字节」**（本仓库第 6 次同类）。
3. **我自己的回滚副本污染了我自己报出的数字。** 首跑报 `151 resolved`；真相是 **152** ——
   备份放在 `.scratch/t24-backup`（在仓内），守卫 `os.walk` 看得见它，
   于是 `event-pipeline-spec.md:153 type-safety.md` 从 resolved 变 ambiguous。
   ⇒ 备份改到仓外 + 守卫排除 `.scratch`，两处都改。**「工具扰动它正在测量的对象」**与 T23 的影子树同型。

## §G 复算命令

```bash
python .scratch/t24-rewrite.py            # 干跑：分类 + 形状闸门 + 扣下清单
python .scratch/t24-rewrite.py --audit    # + 逐条计划表（50 行）
python .scratch/t24-rewrite.py --apply    # 写盘 -> 真守卫 -> 断言 -> 失败则回滚
python .scratch/t24-restore.py            # 从仓外备份按字节恢复（逐文件 sha256）
python .scratch/t24-ablation.py           # §E 的 T0/T1/T2
python .scratch/t24-whatif.py             # §D 的分解（用 HEAD 重建 pre-rewrite 语料）
python scripts/check-spec-refs.py --audit # 守卫权威输出
```

## §H 已知局限（如实记账）

- **41 / 50 仍能说出一个定义在目标文件里的符号**；另 **9** 条现在**只**靠路径解析
  ⇒ 行号去掉后它们**不再有任何「指向某处」的证据**。这是「符号优先」口径的已知代价，
  也是 #675 切片 B/C 的输入（锚门禁需要它自己的消融）。
- **扣下的 1 条仍在**（`error-handling.md:320`）——见 §C。它现在是「看得见的问题」，
  不是「静默消失」。
- **97 条引用仍带行号**：32 条内容锚歧义 + 65 条无锚。它们的行号**照旧会漂**，
  守卫只能 advisory 地报 STALE。⇒ 「符号优先」**不是**全量解法。
- 判定仍**零 CI 覆盖**（`scripts/**`、`.trellis/**` 不在任何 workflow 的 `paths` 内）
  ⇒ 本票与 T21/T22/T23 一样**零 CI 触发**，接 CI 属 #673 切片 C 的独立决策。
