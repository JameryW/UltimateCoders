# T41 —— 把 `check-spec-refs.py` 从单根扩为多根（`.trellis/spec` + `docs`）

> 票面：**#691**。本文所有数字都是 **2026-09-20 在本机 HEAD `ed9f19c` 实测**，不是推断。

## 一、缺口（一手读数，HEAD `ed9f19c`）

守卫的覆盖根是**硬编码单根**：

```python
ROOT = pathlib.Path(__file__).resolve().parents[1]   # :91
SPEC_DIR = ROOT / ".trellis" / "spec"                 # :92
for spec in sorted(SPEC_DIR.rglob("*.md")):           # :464
```

`docs/**` 里同样是**手写的 `path:line` 引用**，同样无人守。
**决定性探针**：只把 `SPEC_DIR` / `_SPEC_PREFIX` 指向 `docs/`，守卫**一字未改**，实测：

| 项 | 读数 |
|---|---|
| 纳入文档 | **8** 个 `.md`（`docs/agents/` 4、`docs/architecture/` 3、`docs/workflows/` 1，另有 `docs/architecture.md`） |
| `path:line` 引用 | **34** = OK 25 + STALE 6 + **PATH_FORM 2** + AMBIGUOUS 1 |
| 无行号路径提及 | **40** = resolved 36 + **DANGLING 2** + MENTION_AMBIGUOUS 2 |

### 1. 已经 fail-closed 的 2 条（`main():684` 的集合含 `PATH_FORM` ⇒ `:789` `return 1`）

| 文档 | 引用 | 实际解析为 | 病因 |
|---|---|---|---|
| `docs/architecture/durable-runtime-migration-assessment.md:121` | `sandbox/agents/claude_code.rs:221` | `crates/uc-engine/src/sandbox/agents/claude_code.rs` | 缺 `crates/uc-engine/src/` 前缀 |
| `docs/architecture/durable-runtime-migration-assessment.md:142` | `uc-types/src/agent.rs:210-232` | `crates/uc-types/src/agent.rs` | 缺 `crates/` 前缀 |

这正是守卫自己的 docstring 拿来举例的形状：
> `PATH_FORM` — a reference WRITTEN WITH A DIRECTORY only resolves as a suffix, i.e. the
> directory is wrong or missing (`uc-engine/src/x.rs` where the file lives at
> `crates/uc-engine/src/x.rs`) ⇒ **判据早已写出来，只是没被用到 docs**。

### 2. 一条会让既有测试变红的（存量，需带理由的豁免）

`docs/agents/domain.md:7` 与 `:10` 提及 `CONTEXT.md`；`CONTEXT.md` 在本仓**不存在**（`git ls-files`
零命中）。而 `tests/python/test_check_spec_refs.py:249` 的
`test_real_corpus_has_no_untriaged_dangling_mention` 断言 `unclassified == []` ⇒ 扩面后**立刻变红**。

**判定为正当提及，不是缺陷** —— 该文档自己写着：

> **When present**, also read:
> * `CONTEXT.md` for canonical domain vocabulary.
> * `docs/adr/` for durable, decision-specific context.
>
> `CONTEXT.md` and `docs/adr/` are **created lazily** by `/domain-modeling` …

⇒ 正确处置是加一条**带理由的 `MENTION_EXEMPT` 规则**（declared 计数 = 2），
而不是放宽那条断言 —— 后者是「新增未分诊提及会红」的**有意断言**。

### 3. 咨询级的 6 + 1 + 2 条（分诊，不必然修）

- **STALE 6**：头号嫌疑 `assessment.md:140 → graph_store.rs:1030 sym=node_type_for`，而
  `node_type_for` 定义在 `:352`，**偏移 −678**（这个幅度不像「定义上方的 doc comment」）。
  其余：`:140 worker_service.rs:262 → dispatch_gate`（+57，定义 319）、
  `:160 worker_service.rs:307 → dispatch_gate`（+12）、
  `:121 python/ultimate_coders/agent/types.py:67 → SubtaskResult`（+230）、
  `:121 sandbox.py:354 → AgentOutput`（−13）、
  `p2-recon.md:21 graph_store.rs:1845 → EventUsage`（+685）。
  ⚠️ 守卫 docstring 已声明 STALE 是**证据给人看，不是门** ⇒ 本票**不把 STALE 升为判词**。
- **AMBIGUOUS 1**：`assessment.md:142 types.py`（2 个候选 ⇒ 裸 basename 欠指定，咨询）。
- **MENTION_AMBIGUOUS 2**：`docs/agents/issue-tracker.md:22 triage-labels.md`、
  `assessment.md:184 task.json`。

## 二、为什么现在没人发现（两条，都实测过）

1. **作用域窄，不是知识缺口。** 守卫对「结构判据看不见漂移」的分析比 docs 的实际情形还准：
   > a census on 2026-09-16 found 149 references of which **0** were out of range, so purely
   > structural checks cannot see the drift at all.

   docs 侧同样是 **0 越界**，但**多出 2 条 PATH_FORM** ⇒ 守卫**能**看见，只是一直没看。
2. **CI 接线成本是零，而非「要新增一套 workflow」。** `check-spec-refs.py` 跑在
   **`ci-scripts.yml:107`（`--audit`）**，该 workflow **刻意没有 `paths`**
   （头注：*NO `paths` filter … the guard walks the whole repository*）
   ⇒ 扩面**不需要改任何 `paths`**（对照 T35 判据：`paths` 对**封闭输入集**是正确的、
   对**全仓游走**是蒙眼布；本守卫的输入集在扩面后仍是开放集，且它所在的 workflow 本就无过滤）。

## 三、判据（沿用既有语义，本票只改**作用域**）

**本票不新增、不删除任何 verdict**，理由是既有的三条结构判据 + 三条咨询判据已经够用，
新增语义会把「扩面」这件事和「改判据」混在一张票里，无法消融。

| 判据 | 类别 | 本票变化 |
|---|---|---|
| `MISSING_FILE` / `PATH_FORM` / `OUT_OF_RANGE` | 结构，fail-closed | 判据不变；**输入集扩到 `docs/**`** |
| `AMBIGUOUS` / `STALE` / `CONTENT_MISMATCH` | 咨询，永不失败 | 判据不变 |
| `DANGLING` / `MENTION_AMBIGUOUS` | 提及，咨询 | 判据不变；`docs` 的 2 条需进豁免表 |

**新增的一条约束（多根特有）**：两张豁免表（`MENTION_EXEMPT` / `SUBJECT_REMOVED`）现在按
「相对 `.trellis/spec/`」的 short 键控（`:595-597` 明写为「避免 `directory-structure.md`
在两个根下同名而碰撞」）。多根后必须保证**两个命名空间不互相污染** —— 即一条
`.trellis/spec` 的豁免规则**不得**匹配到 `docs` 下的同名文件，反之亦然。

## 四、交付形状

| 文件 | 动作 |
|---|---|
| `scripts/check-spec-refs.py` | `SPEC_DIR` → `SPEC_ROOTS`（`.trellis/spec` + `docs`）；`collect()` 遍历双根；`_SPEC_PREFIX` → 前缀表；`_mention_exemption` / `_subject_removed_reason` / `exemption_self_check` 的 `short_of` 按「命中的那一个根」剥离 |
| `docs/architecture/durable-runtime-migration-assessment.md` | 修 2 条 PATH_FORM（补 `crates/…` 前缀）—— **推论 A：过期文档同票修掉** |
| `docs/architecture/durable-runtime-migration-assessment.md` | 分诊后处置 STALE 中的真漂移（至少 `:140` 的 `node_type_for`） |
| `scripts/check-spec-refs.py` | `MENTION_EXEMPT` 增 `docs/agents/domain.md :: CONTEXT.md`（declared 2，理由：文档自称 created lazily / when present） |
| `tests/python/test_check_spec_refs.py` | 多根行为测试 + 每条新行为一次突变 + 真仓读数对账 |
| `.github/workflows/ci-scripts.yml` | 头注补 T41 一段；**不动 `paths`、不动既有 job 的 steps** |

## 五、消融设计（每条新行为一次突变，确认变红）

| # | 突变 | 期望 |
|---|---|---|
| A | 把一个 `docs/` spec 路径写成错目录前缀 | `PATH_FORM`，rc 1 |
| B | 把一个 `docs/` 引用的行号写到文件长度之外 | `OUT_OF_RANGE`，rc 1 |
| C | 把 `docs/` 引用指向不存在的文件名 | `MISSING_FILE`，rc 1 |
| D | 让 `SPEC_ROOTS` 少掉 `docs` | 上述 A/B/C 全部**不再报** ⇒ 证明扩面是 A/B/C 的**必要条件**，不是装饰 |
| E | 把一条 `.trellis/spec` 的豁免规则用于 `docs` 同名文件 | **不得**被匹配（命名空间隔离） |
| F | 删掉 `docs/agents/domain.md` 的 `CONTEXT.md` 提及 | `exemption_self_check` 报「dead rule」⇒ 豁免规则与语料同源 |

D 是**关键**：若 D 不成立，则 A/B/C 测的是别的东西。

## 六、验收（逐条可复算）

1. `python scripts/check-spec-refs.py` 在**修复后** rc 0，且 stdout 的 `scanned N … references in M spec files`
   中 **M 包含 `docs/` 的文档**（`M` 的终态读数在本票实现后据实回填本表）。
2. 在**未修** `durable-runtime-migration-assessment.md` 时同一命令 rc 1，且**恰好 2 条具名 `PATH_FORM`**
   （与探针手算一致，计数逐条对得上）。
3. `python scripts/check-spec-refs.py --audit` 仍打印咨询表，且 **STALE 数量不因扩面而成为判词**。
4. `.venv/Scripts/python.exe -m pytest tests/python/test_check_spec_refs.py -o addopts=""` 全绿。
5. 消融 A–F 各自打红；**D 的「全不报」必须实测**（不是推理）。
6. `ci-scripts.yml` 的 `paths`（不存在）与既有 job 的 steps 未被本票改变；
   其头注含 T41 段。
7. 全仓其余守卫（`check-line-endings` / `check-tasks-refs` / `check-workflow-inputs` /
   `check-readme-ci-table` / `check-journal-ledger` / `check-codex-issue-flow`）仍 rc 0。

### 终态读数（实测 2026-09-20，HEAD `ed9f19c` + 本票工作树）

| # | 验收项 | 实测 |
|---|---|---|
| 1 | 守卫 rc / 语料 | **rc 0**；`scanned 134 path:line references in 13 spec files`；`scanned 267 line-free path mentions in 32 spec files (201 resolved / 43 dangling / 23 ambiguous)` |
| 2 | 扩面即红（未修文档时） | **rc 1**，恰好 **2 条具名 PATH_FORM**，与探针手算逐条对得上 |
| 3 | 咨询表未被升为判词 | `118 ok / 7 stale(advisory) / 9 ambiguous(advisory) / 0 structural failure(s)`；mentions `43 of 267 resolve to no file (43 exempt by documented reason, **0 unclassified**)` |
| 4 | pytest 全量 | `1234 collected = 1223 passed / 1 failed / 10 skipped`（1234 = T40 后基线 1226 + 本票新增 8，数目吻合）。唯一失败 `test_check_journal_ledger.py::test_untracked_journal_is_invisible_to_the_index` **单独复跑 27/27 passed** ⇒ 沙箱 safe-delete 的**累积**计数所致，与 T40 同一根因，非回归 |
| 5 | 消融 | M1 只扫第一根 ⇒ 打红 **9**；M2 关掉命名空间断言 ⇒ 打红 **1**；M3 `_locate_all` 恒空 ⇒ 打红 **4**。三者各有**私有见证**（M2 的唯一红灯只由 M2 打红；M3 的 4 个红灯 2 个独有）⇒ 无装饰分支。恢复后由**独立进程**复算 sha256 = `4bfb8688…` 一致 |
| 6 | CI 接线 | `ci-scripts.yml` **仍无 `paths`**（本票未加）；`jobs` 数不变；改动仅头注 +15 行；`check-workflow-inputs` 报 `10 workflow(s), 9 path-filtered, 20 run-step reference(s)`（与 T40 后态一致） |
| 7 | 其余守卫 | 七道全 rc 0（spec-refs / line-endings / tasks-refs / workflow-inputs / readme-ci-table / journal-ledger / codex-issue-flow） |
| 8 | ruff | 本票改动的 4 个 Python 文件 `All checks passed!` |

**一个刻意的「不验收」**：`STALE` 仍为 7 条咨询。逐条读过一手后判定它们**都是使用点/调用点引用**，不是定义点引用
（如 `graph_store.rs:1845-1849` 是 `EventUsage` 的结构体字面量、`worker_service.rs:262` 是 roster 的文档注释）——
这正是守卫 docstring 预先声明的「小偏移天然歧义，是给人看的证据而非门」。本票**不把它们改成绿**，因为那等于关掉一个正确报警。

## 七、非目标与账（未静默丢弃）

- **不改 `.trellis/spec/**` 的既有判词与阈值**（保证 `spec-refs` job 对旧面的绿/红判据不变）。
- **不把 STALE 升为判词**（docstring 已论证）。
- **不加 `.gitattributes`**、**不动 `ruff format --check`**（另有大票）。
- **记账（不静默）**：T39 起 `check-workflow-inputs.py` 会检查 `run:` 步点名的仓内文件是否在 `paths` 里，
  但 `docs/**` **不会**出现在任何 `run:` 里 ⇒ 该守卫**抓不到**「守卫读了某目录、而 workflow 的 `paths`
  没列它」这一形态。本票不受影响（无过滤），但**该缺口仍存在**，记账待后续票。
- **记账**：`docs/` 下的 `MENTION_AMBIGUOUS`（`triage-labels.md` / `task.json`）本票**只登记不处置**
  —— 它们是欠指定的提及而非缺陷。

## 八、实现期新发现（六处，均已当场处置；判据都是「跑出来的」）

### 1. 🔴 本票**自己**制造了一处「行号合法、内容错误」的漂移，而守卫看不见

recon 文档引了 `scripts/check-spec-refs.py:233`（豁免串所在行）。本票往该文件插入约 65 行后，那串搬到了
**250** —— 行号仍在文件范围内 ⇒ **守卫的两个结构判据都不会管**（`OUT_OF_RANGE` 只管越界），
`--audit` 的 STALE 也只认「命中的符号定义在别处」。**是我人工读一手时发现的。**
⇒ 处置：同票改成 `:250`。⇒ **新账**：*改一个被文档按行号引用的文件后，必须 grep 谁引了它* ——
这是「改文件」与「改引用」之间目前**没有守卫**的一处接缝。

### 2. 文档 §90 的锚点有**三处**是错的，守卫对其中的两处完全沉默

| 原引 | 实际 | 守卫会不会报 |
|---|---|---|
| `graph_store.rs:806-808`（`execution_events` 列） | DDL 实在 **`:920-930`**（`:806-808` 是结构体字面量尾部 + 一段 T10 文档注释） | **不报**（符号未在该行出现 ⇒ 无从算偏移） |
| `graph_store.rs:2290`（「唯一 INSERT 不写这三列」） | 事件 INSERT 实在 **`:2615`**（`:2290` 是 `merge_grants` 邻域） | **不报**（同上） |
| `python/ultimate_coders/agent/types.py:67-80`（`SubtaskResult` 无 usage） | `SubtaskResult` 实在 **`:297`**（`:67` 已是 T15 的 `SubtaskUsage`） | 报 `STALE`（符号在此定义于别处） |
| `graph_store.rs:1030/1038`（graph_nodes 的两个 INSERT 变体） | 两个 INSERT 实在 **`:1167` / `:1175`** | 报 `STALE` |

⇒ **二次印证 docstring 那句「purely structural checks cannot see the drift at all」**：
4 处真错误里只有 2 处能被符号启发式看见，另 2 处**只有读一手代码才能发现**（本票就是读了才发现的）。
另：recon 文档对同一事实引的是 `:928-930`（正确），assessment 引 `:806-808`（错误）——
**两份文档对同一事实给出不同锚点，这本身就是漂移的信号**。

### 3. 同目录内行尾不一致，而且与既有记忆里的清单不符

实测：`scripts/check-spec-refs.py` 是 **CRLF**（797 行），而其邻居 `scripts/check-tasks-refs.py` 是 **LF**；
`tests/python/test_check_spec_refs.py` 是 **CRLF**。故本票的每个写盘脚本都**逐文件声明**自己期望的行尾形态 + 写前断言。
⇒ 既有记忆里「`scripts/*` 是 LF」的说法不准，已按实测修正为「逐文件」。

### 4. 加语料又移动了既有断言（第 N 次同型）

`test_real_corpus_has_no_untriaged_dangling_mention`（断言 `unclassified == []`）会被 `docs/agents/domain.md`
的 2 条 `CONTEXT.md` 立即打红。处置**不是**放宽断言（那是有意断言），而是加一条**带理由、带 declared 计数**的豁免规则
⇒ 复跑 `0 unclassified` 且 `exemption_self_check` 无抱怨。

### 5. 自己的解释性文字也在改语料读数

给 assessment 补的 T15 标记里含 2 条新的 `path:line` ⇒ 引用数 **132 → 134**。
⇒ **文档编辑会改变语料度量**，「扩面后 134」这个数字必须连同它当时的文档状态一起理解。

### 6. `task.py validate` 对 post-archive 的 prd 路径报 not found

`implement.jsonl` 从第一行起就引**归档后**路径（T40 的既定取舍：归档提交才收录任务文件，
而 `check-tasks-refs` 的语料是**已跟踪**集合）。代价是归档前 `validate` 报 1 个 not found。
**CI 不跑 `validate`**（已 grep `.github/workflows/` 确认），故这是已知且可接受的中间态。
