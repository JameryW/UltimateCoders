# T22 research notes —— 可复算的原始记录

所有数字都可用下面两条命令**独立复算**，无需信任本文的叙述。

```bash
# 1) 守卫自身（结构性门禁 + advisory 汇总）
python scripts/check-spec-refs.py
python scripts/check-spec-refs.py --audit     # 附 STALE 明细表

# 2) 全量行（锚点分布由它派生）
python scripts/check-spec-refs.py --json > /tmp/rows.json
```

锚点分类（§A）的判据，直接取自行字段，不含启发式：

```python
symbol  = row["symbol"] is not None
content = row["content_candidate_count"] > 0
# 两者皆假 => 行号是唯一线索
```

---

## §A 锚点分布（148 条）

| 锚点 | 条数 |
|---|---|
| 仅符号 | 25 |
| 仅内容 | 41 |
| 符号 + 内容 | 18 |
| 无锚 | 64 |

可去行号 = 25 + 41 + 18 = **84**。

### 64 条无锚的分布（按文件）

| 文件 | 条数 | 典型形态 |
|---|---|---|
| `agent-capability-spec.md` | 26 | 行号范围（`worker.py:1096-1135`）、语法表 |
| `logging-guidelines.md` | 10 | 「Real examples」列表，代码锚在**上一行** |
| `state-management.md` | 5 | `### Task Creation (…orchestrator.py:149-161)` |
| `error-handling.md` | 6 | 定义点标题（`**TiKV read** (…:100)`） |
| `database-guidelines.md` | 4 | 实现段/索引段标题 |
| `component-guidelines.md` | 1 | `### Example (…query.py:8-51)` |
| `hook-guidelines.md` | 2 | 「Defined in …」「failures are logged but …」 |
| `quality-guidelines.md` | 2 | 工厂/超时段标题 |
| `type-safety.md` | 5 | 一行 4 引用的 `Real examples` + `:58` |
| `codegraph-integration.md` | 1 | docstring 内引用 |
| `worker-service-spec.md` | 1 | 散文续行 |
| （其中 8 条为 `types.py` 裸名歧义，另有 8 条为 `agent.rs`/`types.py` 表格对） | | |

⚠️ **`logging-guidelines.md` 那 10 条最能说明 §B 的价值**：它们引用的
`logger = logging.getLogger(__name__)` 就在**上一行**（第 71 行那段代码块），
本行（`:18` / `:19` / `:25`）只有一个 `path:line`。⇒ 段落级锚能救回的就是这一类。

## §B 段落级锚（本票不实现，仅量化）

窗口 = 从引用行向上/向下走到空行（即 markdown 的「块」）。在 64 条里：

| 结果 | 条数 |
|---|---|
| 段落内找到可匹配代码（可救回） | **21** |
| 段落内也没有 | 35 |
| 无唯一目标（歧义，不适用） | 8 |

⇒ 可去行号 **105 / 148**。

## §C 内容锚的两版对比（为什么必须归一化）

| 版本 | 匹配 | 不匹配 | 判定 |
|---|---|---|---|
| naive（原样串匹配） | 72 | **17** | ❌ 17 里 **13 是假阳性** |
| 归一化（限定名回落 / 去 `=True` 尾 / 去调用括号） | **85** | **4** | ✅ 4 全是语法元变量 |

naive 版的假阳性样本（**这 4 条一条都不该报**）：

```
Worker._derive_capabilities   -> worker.py        实际写的是 def _derive_capabilities
Worker._execute_steps         -> worker.py        同上
Worker._run_single_step       -> worker.py        同上
Worker._emit_step_event       -> worker.py        同上
```

归一化后剩下的 4 条不匹配（**按构造豁免**，因为含 `_` `=` `.` `"` 一个都没有）：

```
!expr        a && b        a \|\| b        (expr)
```

⇒ 它们是 `step_condition.py` 的**语法表元变量**，不是该文件里的代码。

## §D 更宽符号检测的增量（19 → 27）

新增 8 条（**旧检测对这 8 条一律给不出符号**）：

| 规格位置 | 符号 | 来自哪种标记 | 真/噪 |
|---|---|---|---|
| `component-guidelines.md:24` | `Task` | `**bold**` | 真 |
| `component-guidelines.md:38` | `OrchestratorConfig` | `**bold**` | 真 |
| `component-guidelines.md:249` | `Task` | `**bold**` | 真 |
| `component-guidelines.md:265` | `SearchResultItem` | `**bold**` | 真 |
| `state-management.md:89` | `Task` | `### 标题` | 真 |
| `state-management.md:109` | `Subtask` | `### 标题` | 真 |
| `state-management.md:125` | `WorkerInfo` | `### 标题` | 真 |
| `error-handling.md:307` | `delete` | `**bold**`（"for delete operations"） | **噪** |

**消失的：0 条。** ⇒ 这次放宽是**纯增量**，没有把任何原有判定挤掉。

## 消融自检（10/10，`M0`–`M8`）

判据三层：① 探针行的 verdict（走**真 CLI** 子进程 `--json`）；② structural 计数的 **delta**；
③ 对新能力**双跑** —— 同一探针用**改动前**的 `_symbols_on` 再收一次，断言旧检测**会给出不同答案**。

```
baseline: 148 refs, 0 structural, 0 content-mismatch, max spec_line 739, exit 0,
          sha 4408d2850bea...
  [PASS] M0  control: valid ref + code that IS present      OK, 0->0, content_ok=True
  [PASS] M1  dead file                                      MISSING_FILE, 0->1
  [PASS] M2  wrong directory prefix                         PATH_FORM, 0->1
  [PASS] M3  line past EOF                                  OUT_OF_RANGE, 0->1
  [PASS] M4  precedence: symbol + broken path               PATH_FORM, 0->1
  [PASS] M5  content mismatch: quoted code absent           OK, 0->0, content_ok=False
  [PASS] M5b content control: quoted code present           OK, 0->0, content_ok=True
  [PASS] M6  bold-only symbol, defined far away             STALE | old detector: OK
  [PASS] M7  heading-only symbol, defined far away          STALE | old detector: OK
  [PASS] M8  no quoted code at all -> not applicable        OK, 0->0, content_ok=True
restored byte-identically (sha 4408d2850bea...)
```

**为什么 M6/M7 必须双跑**：只断言「新检测报 STALE」**证明不了是放宽起的作用** ——
行内代码里的符号也可能解释它。双跑把「旧检测报 OK」也钉住，才排除了这个替代解释。

**一处过程记录**：M7 第一次跑是 **FAIL**，但**错在探针不在代码** ——
我写的探针是 `Probe: ### SubtaskStatus …`，而 `HEADING_RE` 是**行首锚定**的（真实标题都从第 0 列开始）。
改成行首 `### SubtaskStatus Properties (…)` 后即 PASS。
⇒ 这也**反证**了 `HEADING_RE` 对真实语料是有效的（§D 里 3 条 `### 标题` 锚确实被抓到）。

## 还出现过的两个自我更正（同 T21 的教训，这次在**落笔前**就止住了）

1. **naive 内容匹配的 17 处「失败」里 13 处是假阳性**（见 §C）——
   若直接采纳「加个内容校验器」就一次引入 17 处噪声、其中 13 处要改**没坏**的东西。
2. **我一度把内容不匹配置成一个新的 verdict**，排在 STALE **之后**。
   即：两条**同级 advisory 共用一个字段** ⇒ 同时成立时后者被前者掩盖 ——
   与 T21 抓到的优先级 bug **同型**，只是层级不同。
   ⇒ 改成**独立标志**（`content_ok`），永不与 verdict 互斥；并由 `--json` 同时暴露两个字段。

## 已知局限（如实记账）

1. **1 条散文噪声**：`error-handling.md:307` 的 `delete`（来自 "for delete operations"）。
   未加入 `SYMBOL_STOPWORDS` —— `delete` 完全可能是真实方法名，为一条噪声收窄词表不划算。
2. **段落级锚未实现**（§B），故 35 条真正无锚的引用当前**仍需基线兜底**。
3. **`content_candidate_count` 对歧义引用恒为 0**（`kind == "suffix_ambiguous"` 时不进入计算），
   统计锚点时**必须**把它与「本行真的没引代码」区分开 —— 本文件 §A 的 8 条歧义即此类。
4. 延续 T21 的已知错行：`agent-capability-spec.md:603` 一列两引用，「最近 def」会挑
   `_execute_steps` 而非正确的 `_run_single_step`。
