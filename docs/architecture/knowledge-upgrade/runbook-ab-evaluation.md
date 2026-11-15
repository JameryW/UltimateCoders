# Runbook — A-B 评测：stage B（只采集）对 stage C（恢复视图）

Status: ready-to-run（已跑一轮本地小样本，见 §7；该样本不构成 stage B/C 判读）
Owner: 知识层维护者
Companion: T15（`knowledge/metrics.py`）、T13（签名归一化）、spec-p0 §评测协议

本 runbook 只回答一个问题：**怎么把 stage B/C 的替换跑成一个可判读的配对实验。**
它不产生决策级数据集，也不替任何人下「改善」的结论；可判读数据出来之前，Frontier
里「真实 A-B 数据集」保持 open。

## 1. 前置条件（缺一项结果就不可归因）

- **同一任务集**：任务 id 稳定，两个 arm 跑同一批任务（配对按 `task_id`）。
- **同一模型与采样参数**、**同一 `UC_CONTEXT_BUDGET_TOKENS`**、**同一沙箱镜像**。
- **唯一变量是 `UC_KNOWLEDGE_STAGE`**（`B` 只采集，prompt 逐字不变；`C` 在
  attempt 2+ 注入有界 Recovery View）。
- **每个 arm 一份独立的状态库**：`UC_RUNTIME_STATE_DIR`（SQLite 目录）或
  `UC_DATABASE_URL`（PostgreSQL）。指标只从「该 arm 自己的」`attempt_chain` 与
  `knowledge_evidence` 命名空间算出来，混库等于混 arm。
- 先确认本机没有遗留的库：`--state` 指向的库若为空，指标全 0（这不是 bug，
  而是 stage 未开启或该 arm 没跑过任务）。

## 2. 两个 arm 怎么开

| arm | `UC_KNOWLEDGE_STAGE` | 行为 | 状态库 |
|---|---|---|---|
| B（基线） | `shadow` | 只采集 attempt round / 证据；prompt 与关闭时逐字相同 | `state-b.sqlite3` 目录 |
| C（实验） | `recovery` | B 的全部 + attempt 2+ 读 Recovery View | `state-c.sqlite3` 目录 |

单进程内 stage 是进程级 env，两个 arm 必须**分开启动**（不要在同一 worker 进程
里切换）。可选第三步：先跑 `A`（未设 env）作为绝对对照，验证 B 的 prompt 不变
（比较 `context_manifest.summary()` 的 `total_tokens` 与 prompt 文本一致性）；
A→B 无差异之后再跑 B→C。

```powershell
# arm B
$env:UC_KNOWLEDGE_STAGE = "shadow"
$env:UC_RUNTIME_STATE_DIR = ".uc/ab/b"
# 跑任务集 ... 完成后清场再开 arm C
$env:UC_KNOWLEDGE_STAGE = "recovery"
$env:UC_RUNTIME_STATE_DIR = ".uc/ab/c"
```

## 3. 采集与比较

```powershell
# 单臂概况（含失败签名直方图，用来校准签名粒度）
python -m ultimate_coders.knowledge.metrics --state .uc/ab/b --signatures
python -m ultimate_coders.knowledge.metrics --state .uc/ab/b --json --signatures

# B 对 C 的配对比较
python -m ultimate_coders.knowledge.metrics --baseline .uc/ab/b --variant .uc/ab/c --json --signatures
```

看结果前先看三个健全性字段：

1. `paired_tasks`：配对任务数，即可比样本量；**< 2 直接 `insufficient_data`**。
2. `baseline.attempts_with_usage` / `variant.attempts_with_usage`：token 指标只统计
   实际上报用量的 attempt；为 0 时 `tokens_per_subtask` 是 `null`，不要把它当 0 用。
3. `signatures`：两个 arm 的失败签名分布是否同粒度（同一类错误不应在一边塌成
   一条、另一边裂成十条；若是，先回到 T13 调整归一化再判读）。

## 4. 判读规则（spec-p0 的判定词表，逐条对应）

- `insufficient_data`：配对任务 < 2 或两臂任务 id 没有交集 → 修任务集，不是改代码。
- `regressed`：成功率下降 → 先停；stage C 不得在成功率下降时进入 stage D/E。
- `inconclusive`：token 劣化超过 `TOKEN_TOLERANCE=0.05` → 查 Recovery View 预算
  （`UC_RECOVERY_BUDGET_TOKENS`，默认 600）与是否有重复注入。
- `improved`：成功率非劣 + 平均 attempt 或重复失败签名率下降 + token 不劣化。
- `non_inferior`：成功率不降但 attempt/重复率也没降 → 允许继续，但不能宣称收益。

**两轮同向**：以两个独立跑次的方向一致作为进入 stage D/E 的前提；单次结果只作为
下一轮的假设。检索命中率、压缩率不能单独判优（只能解释差异）。

## 5. 常见故障

| 现象 | 多半因为 |
|---|---|
| 指标全 0 | stage 未开（`A`/空），或该 arm 的状态库目录指错 |
| `paired_tasks = 0` | 两臂任务 id 不同（重新生成 id / 混库） |
| `tokens_per_subtask = null` | 模型路由没有上报 usage，不是 0 消耗 |
| 签名直方图只有一条 | 归一化过粗（路径/数字已折叠）；对照原始 excerpt 判断是否需要更细的 pattern |
| 重复失败率不降反升 | Recovery View 预算过大挤掉其它证据，或恢复视图重复了已在 trace 里的信息 |
| 重发 attempt 1 后没有新 attempt round、也没有 sandbox 日志 | 该 subtask 的 attempt 0 已成功；`_outcome_is_settled` 按设计不再重跑成功结果。要观测 Recovery View，任务集必须先在 attempt 0 失败 |

## 6. 报告模板（用于往 map/assessment 回填）

```text
commit <sha>  model <route>  tasks <N=20, ids 稳定>
arm B: stage=shadow  state=<path>  attempts=<n> success_rate=<x> avg_attempts=<y>
arm C: stage=recovery state=<path> attempts=<n> success_rate=<x> avg_attempts=<y>
verdict=<improved|non_inferior|inconclusive|regressed|insufficient_data>
paired=<k>  repeated_failure_rate B=<x> C=<y>  tokens_note=<reported / not reported>
run 1 direction=<...>  run 2 direction=<...>  -> 是否进入 stage D/E = <yes/no>
```

报告里**禁止**放 prompt 正文、diff 正文或任务描述；签名是归一化后的字符串，可以放。

## 7. 首次真实运行记录（2026-11-15，本地小样本，不构成 stage B/C 判读）

这一轮的目的**不是**判优，而是验证「真实 worker + 真实模型」能把证据、usage 与
配对指标跑通；数据本身样本太小，且没有触发 stage C 的恢复视图。

- 环境：本地 worker（`UC_CODING_AGENT=local-harness`，Ollama
  `openai/qwen3.8-9b-uc`，`host=127.0.0.1:11434`）通过 SSH 隧道连远端
  gateway/NATS；项目 `.uc/ab/work`（独立 git 仓库，commit `535711f`）；
  两臂 state 目录 `.uc/ab/b4`（`shadow`）与 `.uc/ab/c4`（`recovery`）。
- 任务集：`ab-real-4` / `ab-real-5` / `ab-real-6`，任务 id 与描述两臂一致
  （分别创建 `ab_a.txt`=`alpha`、`ab_b.txt`=`beta`、`ab_c.txt`=`gamma`），
  每任务一次 dispatch（attempt 0），`required_capabilities=["code"]`。
- 结果（`python -m ultimate_coders.knowledge.metrics --baseline .uc/ab/b4
  --variant .uc/ab/c4 --json --signatures`）：

  ```text
  paired_tasks = 3
  arm B: attempts 3, attempts_with_usage 3, success_rate 1.0, avg_attempts 1.0,
         tokens_per_attempt 997.67 (2993 reported)
  arm C: attempts 3, attempts_with_usage 3, success_rate 1.0, avg_attempts 1.0,
         tokens_per_attempt 2188.33 (6565 reported)
  verdict = inconclusive   reason = tokens_worse
  signatures: baseline [], variant []   (no failed attempt)
  ```

- 读法：这一轮只能证明 A/B harness 在真实执行上工作（usage 有上报、配对按
  `task_id`、`--state` 目录可解析、两个签名直方图同为空），**不能**证明
  stage C 有优劣。逐任务对比可以看到同样的 attempt 0 任务在两臂间出现
  >2× 的 token 波动（例如 `ab-real-6` 输入 508 vs 3491），说明单次采样的
  模型噪声远大于本样本量。
- 关键限制：三个任务在 attempt 0 **全部成功**，而成功的 subtask 重发会被
  `_outcome_is_settled` 结算（不重跑），所以 stage C 的 `_recovery_context_block`
  从未被调用。要测 C，任务集必须包含 attempt 0 失败的重试，并把失败 attempt
  在两臂配对；然后再跑第二轮验证方向一致。
- 工具链修复（本轮顺带发现并修）：`harness_local_loop.py` 现在把子进程逐轮
  `usage` 事件求和为 `TokenUsage`（未上报则 `None`，不伪造 0），
  `metrics.load_state` 接受 runbook 的 state 目录并解析到 `state.sqlite3`。

## 8. 失败注入验证 stage C 恢复视图（2026-11-15，方法验收，不是性能数据）

目的：验证 attempt 0 失败后重试路径能真正编译并注入 Recovery View，而不是
只看「重发成功任务被结算」的静态路径。这一轮同样**不是** B/C 判优。

- 方法：两臂各起一个 worker，任务 id 都是 `ab-fail-1`（文件约束
  `ab_fail.txt`），但 `OPENAI_API_BASE=http://127.0.0.1:9/v1` 故意不可达，
  让每次 attempt 都在模型连接处失败；state 为 `.uc/ab/fail-b`（`shadow`）与
  `.uc/ab/fail-c`（`recovery`），其余环境同上一节。
- 结果：两臂都产生 3 条 `attempt_chain`（attempt 0/1/2，各自 0 patch）与
  3 条 `failure_signature`；两臂 `attempts=3`、`attempts_with_usage=0`、
  `success_rate=0`、`repeated_failure_rate=0.6667`，`tokens_per_attempt=null`
  （失败发生在模型调用前，没有 usage，诚实为空），配对 `paired_tasks=1`
  → `insufficient_data`。
- 关键证据：从 JetStream `UC_TASK_EVENTS` 的 `subtask_retry` 事件读回：

  ```text
  arm C attempt 1: recovery {rounds_shown: 1, rounds_available: 1,
                             evidence_records: 0, tokens: 95, budget_tokens: 600}
  arm C attempt 2: recovery {rounds_shown: 2, rounds_available: 2,
                             evidence_records: 0, tokens: 184, budget_tokens: 600}
  arm B attempt 1: recovery None
  arm B attempt 2: recovery None
  ```

  即 B 臂重试保持基础 prompt；C 臂每次重试都把此前轮次编译进恢复视图，并在
  600 token 预算内递增（1→2 轮，95→184 token），未触发截断。
- 读法：`_outcome_is_settled` 只结算**成功**结果，失败重试会正常进入
  `_recovery_context_block`；两条失败签名完全相同是因为根因同是模型连接错误，
  这正说明当前归一化在该类错误上偏粗（可作为 T13 校准的对照组），不能据此
  判定 C 有改善。要判优，仍需两臂各自包含成功与失败混合、且失败 attempt 在
  同一任务集内配对，并跑第二轮同向。

## 9. 带 usage 的失败混合注入（2026-11-15，判读管线验收，仍不是性能数据）

目的：让失败 attempt 也带 token usage，并让两臂各有一个成功样本，从而验证
`--signatures` 归一化、`pair` 口径与 `verdict` 计算在**真实执行**上能走通。
这一轮故意把任务做窄，**不能**当作 stage B/C 优劣结论。

- 方法：任务 `ab-mix-1`（`mixed_a.txt`=`alpha`）与 `ab-mix-2`
  （`mixed_b.txt`=`beta`），`agent_config={"max_turns":1}`，用正常可达的
  Ollama `openai/qwen3.8-9b-uc`；`max_turns=1` 让模型通常来不及 `write_file`
  再 `done()`，从而产生带 usage 的失败重试。两臂 state 为 `.uc/ab/mix-b`
  （`shadow`）与 `.uc/ab/mix-c`（`recovery`），项目仓库在第二臂前重置到
  `535711f`。
- 结果（`python -m ultimate_coders.knowledge.metrics --baseline .uc/ab/mix-b
  --variant .uc/ab/mix-c --json --signatures`）：

  ```text
  paired_tasks = 2
  arm B: attempts 5, attempts_with_usage 4, success_rate 0.5, avg_attempts 2.5,
         repeated_failure_rate 0.5, tokens_per_attempt 574.0 (2296 reported)
  arm C: attempts 6, attempts_with_usage 6, success_rate 0.0, avg_attempts 3.0,
         repeated_failure_rate 0.6667, tokens_per_attempt 652.67 (3916 reported)
  verdict = regressed   reason = success_rate_lower
  signatures: baseline [{signature: "Stopped after max turns (<n>) without done()", count: 4}]
              variant  [{signature: "Stopped after max turns (<n>) without done()", count: 6}]
  ```

  C 臂的 `subtask_retry` 事件仍逐次递增恢复视图：attempt 1
  `rounds_shown=1, tokens=67`，attempt 2 `rounds_shown=2, tokens=127`
  （budget 600）；B 臂两次都是 `recovery None`。
- 读法：`regressed` 是注入方式的**伪信号**——`max_turns=1` 让任务普遍不可完成，
  B 的 0.5 success 里还有一次是模型单轮直接 `done()` 的捷径（exit 0 但无
  usage），两臂只是随机噪声。可确认的是管线本身：失败 attempt 能带 usage、
  失败签名被归一化成 `<n>` 并跨 attempt 聚合、`paired_tasks=2` 时能算出
  `regressed`、恢复视图在真实重试里注入。
- 下一步：用正常 `max_turns`、任务语义完整、同时包含成功与失败重试的任务集
  各跑一轮（两轮同向才可判读）；本文两节只作为方法与工具链证据保留。

## 10. 真实工作流语料小跑（2026-11-15，管线验收，仍不是性能数据）

目的：用 AwesomeWorkflows 模板中裁剪出的**真实工作流任务**，验证「真实执行 →
确定性产物验收 → usage/失败签名 → 配对指标」这条链路，而不是只看模型自述的
`done()`。语料仍是缩小版，**不能**当作 stage B/C 的判读结论。

- 语料：从 `C:\Users\jamer\aiworks\AwesomeWorkflows` 的 `business-document`、
  `data-report` 模板裁剪出 2 步任务（`awf-doc-1`、`awf-report-1` / `awf-report-3`），
  外加保留未跑的 `awf-site-1`（`website-building`）。任务通过
  `.uc/ab/wf_corpus.py` 直接发到 `uc.subtask.execute`，`file_constraints` +
  `steps` 让 worker 走 worktree 与 step 链；验收脚本 `.uc/ab/wf_validate.py`
  检查产物文件、关键数字和必需句子。
- 首轮问题与修正：
  - 测试仓库默认分支是 `master`，而 `WorkspaceManager.base_branch` 默认 `main`，
    导致 release 后 `main..HEAD` 为空、产物被当作 `no_changes` 丢掉。三个测试
    仓库改名为 `main` 并快进后，doc 任务首次 7/7。
  - 三个任务都写根目录 `brief.md`，后跑的任务会覆盖前一个任务在 base branch
    上的 brief；因此验收必须在每个任务完成的当下做，或按任务分目录。
  - `max_turns=4` 的 report 任务在 593s 后触发
    `[step <n> (local-harness) failed] Stopped after max turns (<n>) without done()`；
    `max_turns=8` 的 report 任务在 step 1+step 2 超过 900s subtask timeout 时被
    `Subtask timed out after 900s` 终止。语料最终固定 `max_turns=8`、
    `timeout_seconds=1800`。
- B 臂（`shadow`，`.uc/ab/wf-b2`，仓库 `.uc/ab/wf-work-b`）：
  - `awf-doc-1` 成功，7/7，commit `5309748`。
  - `awf-report-1`（max_turns 4）失败并重试，计入失败样本；`awf-report-2`
    （900s timeout）失败并重试；`awf-report-3`（1800s）成功，9/9，commit
    `b6e6e0a`。
  - 指标：attempts 4、attempts_with_usage 3、success_rate 0.5、failures 2、
    tokens_per_attempt 2300.67（6902 reported）、tokens_per_subtask 1725.5；
    签名 `Subtask timed out after 900s` 与
    `[step <n> (local-harness) failed] Stopped after max turns (<n>) without done()`。
- C 臂（`recovery`，`.uc/ab/wf-c2`，仓库 `.uc/ab/wf-work-c`）：
  - `awf-doc-1` 成功（原验收 6/7，仅因模型写 `three days per week` 而非
    `3 days in office`；放宽同义检查后 7/7），commit `5a6614c`。
  - `awf-report-3` 成功，9/9（`units sold multiplied by price per unit` 被
    同义检查接受），commit `fedf24d`。
  - 指标：attempts 2、attempts_with_usage 2、success_rate 1.0、
    tokens_per_attempt 1193.5（2387 reported）。
- 配对（`--baseline .uc/ab/wf-b2 --variant .uc/ab/wf-c2 --json --signatures`）：
  `paired_tasks=2`、`verdict=non_inferior`，baseline
  `tokens_per_attempt=1580.5`（3161 reported），variant `1193.5`（2387 reported）；
  baseline 两个失败签名，variant 无签名。
- 读法：两条配对任务在 C 臂的 attempt 0 都成功，而
  `_outcome_is_settled` 不重跑成功结果，所以 **stage C 的恢复视图没有被注入**；
  两臂在配对任务上实际是同一行为，token 差异只是模型输出长度波动，
  `non_inferior` 不能归因于 stage C。B 臂的 timeout / max-turns 失败不在 C 臂
  数据集内，也不参与配对。可确认的是：真实 workflow steps 能执行并留下
  worktree/commit/attempt_chain；确定性产物检查能独立于 `done()` 判断结果；
  usage、失败签名和 paired verdict 在真实语料上继续工作；timeout 与
  `max_turns` 必须按 9B 模型的实际步耗时设置。
- 结论：这是对工作流语料与验收方法的管线验收，仍不是决策级 A/B 数据。
  下一步仍是：正常 `max_turns` 下同时覆盖成功/失败 attempt 的成对任务集，
  同向跑两轮，并修掉「同一路径产物相互覆盖」的语料设计问题。
