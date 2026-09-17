# T25 侦察笔记 —— #675 切片 B：47 处悬空提及的逐条分类与处置

> 全部命令在本仓根执行；涉及守卫内部行为的一律**复用守卫自己的函数**（importlib 加载
> `scripts/check-spec-refs.py`），不另写第二套 `os.walk` —— 否则就是「换个作者的同一类间接信号」。

## 0. 复算入口（先跑这三条，再读结论）

```bash
.venv/Scripts/python.exe scripts/check-spec-refs.py --audit      # 47 悬空 + summary
.venv/Scripts/python.exe scripts/check-spec-refs.py --json > .scratch/t25-rows.json
.venv/Scripts/python.exe .scratch/t25-triage.py                    # 逐条附散文 + basename 证据
```

## 1. 守卫口径复核：47 是**哪个** 47

首版我用 `not r["target"]` 过滤，得到 **68** —— 与守卫自报的 47 不符。**先怀疑自己的谓词，而不是质疑守卫**：
读源码后确认判据是 `verdict == DANGLING`，而 `DANGLING` 对应 `kind == "none"`（**candidates 为空**）。
那多出的 **21** 条是 `MENTION_AMBIGUOUS`（`target` 为 `None` 但 **candidates 非空**，≥2 个候选）。
⇒ `47 (DANGLING) + 21 (AMBIGUOUS) = 68`，两个数**各自都对**，差别在谓词。
**独立复算**：`Counter(r["verdict"] for r in mentions)["DANGLING"] == 47`（走 JSON 口径，与守卫 `len(dangling)` 同值）。

**这是本仓「自写汇总脚本也是间接信号」的第 N 次实例**，但形态是新的：不是脚本算错，
而是**我用了一个自认为等价、实则更宽的谓词**。判据：**拿别人的工具的数去核对时，先读它的判据，不要自己猜。**

## 2. 结论 A：没有一处能靠「同名文件换路径」修好

对 34 个 distinct `(spec, ref)` 逐个查 `_repo_index()[basename]`，**34/34 全为空**
（即 basename 在本仓任何深度都不存在）。⇒ 「改指向新位置」不可能靠机械替换完成。

⚠️ **这条否定了一个看似显然的对策**（「找到搬走的文件、把路径改过去」），
避免把整张票写成一次注定无解的机械替换。

## 3. 结论 B：`tui/**` 不是「另一个仓库」，是**在本仓被删除**

| 断言 | 命令 | 实测 |
|---|---|---|
| `tui/` 曾被跟踪 | `git log --oneline --diff-filter=A -- "tui/*"` | 有 `feat(tui):` 提交（`25abae8` 等） |
| `tui/` 已不在 HEAD | `git ls-tree -r --name-only HEAD \| grep -c '^tui/'` | `0` |
| 删除提交 | `git log --diff-filter=D -1 -- tui/src/reducer.ts` | **`d7f4631`** |
| 删除理由（一手正文） | `git log -1 --format=%b d7f4631` | *"Delete tui/ directory (Ink/React TUI no longer needed)"*、*"Delete .github/workflows/ci-tui.yml"* |
| 规模 | `git show --stat d7f4631` | 84 files changed, 2081(+), **15336(−)** |
| 替代物在仓内 | `git ls-tree -r --name-only HEAD \| grep '^packages/uc-orchestrator/src/'` | `extension.ts`、`ui/{progress-widget,status-formatter,status-renderer,subtask-tree-overlay,task-list-overlay,task-result-renderer}.ts`、`orchestrator/events.ts` |
| **spec 未同步** | `git show d7f4631 --name-only \| grep -i tui-grpc` | **空** |
| spec 最后修改 | `git log -1 --format='%h %ad' --date=short -- .trellis/spec/frontend/tui-grpc-spec.md` | `b897a7d` **2026-06-23**（**早于**删除的 06-25/06-26） |

`local-worker-bridge-spec.md` 同理：`a368371`（2026-06-27）删掉 `crates/uc-grpc/src/local_worker.rs`
与 `python/ultimate_coders/local_worker.py`（**同一提交**），理由 *"replace hand-rolled JSON/gRPC with
connectrpc gRPC-Web client (#171)"*。

⇒ 两篇 spec 描述的都是**已从本仓删除**的子系统。**处置从「豁免」改为「加状态横幅」** ——
两者都对外宣称「这些路径不是缺陷」，但只有横幅**基于事实**。
（#675 票面与守卫 docstring 都写了「out-of-repo」，本票一并更正。）

## 4. 7 类划分（完全且不相交）

机械化校验（脚本内 `assert`）：类计数 `{1:29, 2:2, 3:4, 4:5, 5:1, 6:4, 7:2}`，
**未分类 0**，合计 47。⇒ 这张划分是**总且互斥**的，不是「挑了 7 个例子」。

### 4.1 逐行清单（机器生成，勿手改）

### 类 1
- `backend/taskservice-grpc-spec.md:60`  `tui/src/grpc/client.ts`
- `backend/taskservice-grpc-spec.md:308`  `tui/src/grpc/types.ts`
- `frontend/tui-grpc-spec.md:17`  `tui/src/grpc/client.ts`
- `frontend/tui-grpc-spec.md:32`  `tui/src/hooks/useGrpcClient.ts`
- `frontend/tui-grpc-spec.md:57`  `tui/src/hooks/useTaskEvents.ts`
- `frontend/tui-grpc-spec.md:118`  `tui/src/components/CjkTextInput.tsx`
- `frontend/tui-grpc-spec.md:134`  `cjk-input-utils.ts`
- `frontend/tui-grpc-spec.md:143`  `cjk-input-utils.ts`
- `frontend/tui-grpc-spec.md:145`  `tui/src/cjk-input-utils.ts`
- `frontend/tui-grpc-spec.md:165`  `tui/src/components/StatusBar.tsx`
- `frontend/tui-grpc-spec.md:195`  `tui/src/reducer.ts`
- `frontend/tui-grpc-spec.md:350`  `reducer.ts`
- `frontend/tui-grpc-spec.md:351`  `keymap.ts`
- `frontend/tui-grpc-spec.md:352`  `formatters.ts`
- `frontend/tui-grpc-spec.md:353`  `symbols.ts`
- `frontend/tui-grpc-spec.md:354`  `truncate.ts`
- `frontend/tui-grpc-spec.md:355`  `filter.ts`
- `frontend/tui-grpc-spec.md:356`  `cjk-input-utils.ts`
- `frontend/tui-grpc-spec.md:357`  `chatlog-utils.ts`
- `frontend/tui-grpc-spec.md:359`  `tui/vitest.config.ts`
- `frontend/tui-grpc-spec.md:547`  `tui/src/symbols.ts`
- `frontend/tui-grpc-spec.md:562`  `tui/src/keymap.ts`
- `frontend/tui-grpc-spec.md:658`  `keymap.ts`
- `frontend/tui-grpc-spec.md:670`  `reducer.ts`
- `frontend/tui-grpc-spec.md:671`  `formatters.ts`
- `frontend/tui-grpc-spec.md:672`  `symbols.ts`
- `frontend/tui-grpc-spec.md:673`  `truncate.ts`
- `frontend/tui-grpc-spec.md:674`  `filter.ts`
- `frontend/tui-grpc-spec.md:675`  `cjk-input-utils.ts`

### 类 2
- `backend/local-worker-bridge-spec.md:17`  `crates/uc-grpc/src/local_worker.rs`
- `backend/local-worker-bridge-spec.md:47`  `python/ultimate_coders/local_worker.py`

### 类 3
- `backend/scheduler-spec.md:327`  `uc.scheduler.yaml`
- `backend/scheduler-spec.md:329`  `uc.scheduler.yaml`
- `backend/scheduler-spec.md:435`  `uc.scheduler.yaml`
- `backend/scheduler-spec.md:436`  `uc.scheduler.yaml`

### 类 4
- `backend/directory-structure.md:127`  `new_module/impl.rs`
- `frontend/directory-structure.md:77`  `rate_limiter.py`
- `guides/cross-layer-thinking-guide.md:90`  `record-session.md`
- `guides/cross-layer-thinking-guide.md:99`  `record-session.md`
- `guides/cross-layer-thinking-guide.md:134`  `index.json`

### 类 5
- `backend/directory-structure.md:72`  `docker.rs`

### 类 6
- `backend/nats-bridge-spec.md:353`  `tests/python/test_dashboard.py`
- `backend/nats-bridge-spec.md:353`  `tests/python/test_nats_worker.py`
- `backend/quality-guidelines.md:148`  `tests/python/test_agent.py`
- `frontend/quality-guidelines.md:9`  `tests/python/test_agent.py`

### 类 7
- `backend/dashboard-spec.md:116`  `python/ultimate_coders/agent/scheduler.py`
- `backend/scheduler-spec.md:100`  `python/ultimate_coders/agent/scheduler.py`

### 4.2 每类的证据与处置

| 类 | 行 | 证据（commit / 实测） | 处置 |
|---|---|---|---|
| **1. 已删子系统 TUI** | 29 | `d7f4631`；替代物 `packages/uc-orchestrator/src/**` 在仓 | `tui-grpc-spec.md` + `local-worker-bridge-spec.md` 加横幅；`taskservice-grpc-spec.md` 加小节注解 |
| **2. 已删子系统 local-worker bridge** | 2 | `a368371`（两文件同提交删除） | 同上 |
| **3. 运行时/运维提供** | 4 | `scheduler-spec.md:329` 自述 *"Missing file = idle scheduler (opt-in)"*，`UC_SCHEDULER_CONFIG` env → `./uc.scheduler.yaml`；`:435/:436` 失败表也用同一口径 | **豁免**（reason 写进守卫） |
| **4. 举例 / 命名规范示例** | 5 | `new_module/impl.rs`（"e.g.," 引导）、`rate_limiter.py`（表格 Example 列）、`index.json`（"e.g., checking if … exists" 的通用探针例）、`record-session.md` ×2（Trellis 跨平台命令模板） | **豁免** |
| **5. 目录表列了已删文件** | 1 | `514ec3b`（2026-07-12）*"remove dead DockerSandbox and UC_SANDBOX_MODE knob (#237)"*；`ls crates/uc-engine/src/sandbox/` = `agents/ file_tracker.rs mod.rs pool.rs subprocess.rs`（**无 docker.rs**） | **改 spec**（该表是「当前目录」清单，必须为真） |
| **6. 测试文件指针已删** | 4 | `47f2add`（2026-06-26）*"consolidate repo structure (round 2) (#163)"*，三文件均 `D`（**非 rename**；该提交的 `R100` 只在 docker 文件上） | **改 spec** |
| **7. Python → Rust 迁移** | 2 | `15b5ae3`（2026-08-04）*"remove dead Python scheduler.py after Rust SchedulerService activation (#548)"*；`crates/uc-python/src/scheduler.rs:1` = *"PySchedulerService — Python-facing Scheduler class."*，`lib.rs:37` 注册 `PySchedulerService` | **改 spec**（重指 uc-python） |

## 5. 类 6/7 的「改成什么」——两处**不能**照抄票面的诱导

### 5.1 `nats-bridge-spec.md:353`：不只是路径失效，**整张测试清单都失效**

票面只说两个文件不存在。实测**更严重**：该小节 9 个测试名（`:357–:365`）在 `tests/python/` 里
**一个都不存在**（松匹配 `submit_payload|nats_heartbeat|publish_update_failure|nats_submit_fallback|heartbeat_payload`
仅命中 `test_nats_jetstream_subtask.py:513` 的**另一个**名字 `test_execute_and_report_acks_on_publish_update_failure`）。
⇒ 该段是**整段**过期（文件指针 + 清单）。处置：重指**真实存在**的 NATS 测试文件，
并对失效清单加一句「早于 2026-06 重组」的说明；**不臆造**逐条替代测试名。

### 5.2 `dashboard-spec.md:116`：批注指向 `uc-python` 会是**半真**

该小节原文是 `class Scheduler: def trigger_job(self, task_id: str) -> bool  # Manually trigger a scheduled job`。
实测 `PySchedulerService`（`crates/uc-python/src/scheduler.rs:315`，`#[pymethods]` 在 `:319`）的方法集是
`new / create_cron_job / create_one_shot_job / cancel_job / list_jobs / get_job / get_execution_history /
set_night_window / clear_night_window / start / stop / is_running`（各带 `_async` 变体）—— **没有 `trigger_job`**。
且 `python/ultimate_coders/agent/orchestrator.py:143` 写着 `self.scheduler = None`（**永为 None**），
而 `dashboard/app.py:664` 的 `/dashboard/api/scheduler/jobs/{id}/trigger` 端点依赖 `orch.scheduler.trigger_job(...)`
⇒ **该端点恒返回 503 "Scheduler not available"**。
⇒ 处置：把该小节标注为**已移除**，并写清现居地（Rust）与端点的真实行为；**不把** `trigger_job` 说成还在。

⚠️ 这正是 §铁律「不臆造」的具体形态：**「改指向新位置」在目标文件里找不到被文档化的那个方法时，
就只能写成「已移除」，不能只换个路径让守卫变绿。**

## 6. 豁免机制的设计（守卫侧）

```
MENTION_EXEMPT = ((spec, ref-glob, reason), ...)      # 细粒度：题面级
SUBJECT_REMOVED = {spec: (removal_commit, successor)} # 整篇级：正文必须点名提交
```

- 整篇豁免**只有在 spec 正文里出现那枚提交哈希时才被承认** ⇒ 横幅不能忘、豁免不能静默。
- **自检两条**，把「装饰性豁免」钉死：
  1. 每条 `MENTION_EXEMPT` 规则必须**命中 ≥1 条**实时提及（豁免不到东西的规则 = 对语料的谎报）；
  2. 每个 `SUBJECT_REMOVED` 的 spec 必须**含其删除提交哈希**，且该 spec 下**真的还有**悬空提及。
- 自检结果作为 **advisory 段落**打印（**不参与 exit code**，满足票面「不改 exit code」），
  并由 `.scratch/` 的消融脚本断言「突变后会报警」。

## 7. 消融实测（一次一处突变；恢复后校 sha256）

`M0` 的基线与 `M5` 的恢复态**逐字节相同**；**六次运行全部 exit 0**（advisory 语义未被削弱）。
所有数字读自守卫自己的 stdout（不是重算 —— 重算是第二个会分歧的 oracle）。

| # | 突变 | exit | exempt | unclassified | 自检问题 | 结论 |
|---|---|---|---|---|---|---|
| **M0** | 不改 | 0 | 40 | 0 | 0 | 基线干净 |
| **M1** | 从 `tui-grpc-spec.md` 横幅删掉提交令牌 `d7f4631` | 0 | 13 | **27** | **1** | 整篇豁免**确实**以「spec 自己声明」为条件 —— 横幅不是装饰 |
| **M2** | 把 `uc.scheduler.yaml` 规则改成 `uc.scheduler.yamls`（匹配不到） | 0 | 36 | **4** | **1** | 死规则被**报出来**，不是静默放过 |
| **M3** | 在 `directory-structure.md` 围栏外注入 `` `totally_missing_zzz.py` `` | 0 | 40 | **1** | 0 | 豁免集**不会**吞掉无关提及；且注入的提及被守卫真的看见了（`228` 提及） |
| **M4** | （承 M3）把该 spec 的规则 pattern 放宽成 `*` | 0 | **41** | **0** | **1** | ⚠️ **放宽后那条注入的提及被静默豁免** ⇒ 风险是真的；**首轮它一个问题都不报** |
| **M5** | 全部恢复 | 0 | 40 | 0 | 0 | 与 M0 逐字节相同（三个文件 sha256 相等） |

### M4 的产出：一个被实测出来的洞，以及补上的判据 3

**M4 首跑是本票唯一一处「设计自以为严密、实测不严密」的地方。** 把细粒度规则的 pattern 从
`new_module/impl.rs` 放宽到 `*` 后，它**静默变成整篇豁免**，而 `exemption_self_check` 的两条
原有判据都不触发（规则仍然「命中 ≥1 条」，横幅也都在）—— 同时 `--audit` 依然报告
`0 unclassified`，也就是**一条看起来完全健康的输出**。

处置：补**判据 3** —— `MENTION_EXEMPT` 的 pattern **不得是裸通配符**（`*` / `**` / `*.*`）。
整篇级豁免必须走 `SUBJECT_REMOVED`（在那里它必须由 spec 正文自己声明）。补完后 M4 报
`self_check_problems=1`，而 M0/M1/M2/M3/M5 的结论全不变。

⇒ 这条判据**检查的是 pattern 的形态，而不只是它的命中数** —— 与 T22 那条「凡 `f(x) == g(x)`
的不变量，先确认两边定义不互相调用」同源：**只数命中数的检查器，看不见「一条规则比它的理由更宽」。**

## 7b. 交付后的实测数字（收口时复算）

```
scanned 98 `path:line` references in 11 spec files
scanned 227 line-free path mentions in 26 spec files (166 resolved / 40 dangling / 21 ambiguous)
summary: 89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural failure(s)
         0 of 98 have no matching quoted content (orthogonal to the verdict above)
         mentions: 40 of 227 resolve to no file (40 exempt by documented reason, 0 unclassified)
spec reference audit passed.        <- exit 0
```

- **47 -> 40** 悬空；**40 == exempt**；**0 unclassified**。
- 提及总数 **221 -> 227**（+6 净）：修复时引入的**真实**指针（`+13 resolved`）多于被删掉的死提及（`-7 dangling`）。
- **既有 ref 判定一格未动**：`89 ok / 1 stale / 8 ambiguous / 0 structural` 与 T24 收口逐项相同。
- 豁免来源：`SUBJECT_REMOVED` 29 条（tui-grpc 27 + local-worker 2）、`MENTION_EXEMPT` 11 条
  （`uc.scheduler.yaml` 4 + `tui/src/**` 2 + `record-session.md` 2 + 其余 3 各 1）。
- `git diff --numstat`：9 篇 spec 全为小改动（最大 `10/17`，来自被整段替换的 20 行块），
  守卫 `198/16`。**无整文件重写**；9 篇 spec 改动后**孤立 LF 均为 0**。
- 门禁：`cargo fmt --all --check` clean（Rust 未动）、`ruff check` clean；
  `ruff format --check` **改动前后都失败**（用 `git show HEAD:` 的版本取证：既有的
  `EXCLUDE_DIRS` / `CODE_EXT` 两块就是不允许被格式化的），`scripts/**` 不在 CI lint 面内。

## 8. 已知限制

1. **豁免是「人工裁定」的固化，不是自动判据。** 它把「我认为这 40 处不是缺陷」变成可审计、可复算的
   声明，但**判据本身仍是人写的**。切片 C 若要门禁，门禁的强度上限 = 这份表的质量。
2. **`os.walk` 视图 ≠ git 视图**（T24 已记）：仓内未跟踪的临时产物会进索引。
   `.scratch` 已在 `EXCLUDE_DIRS`，但**任何新的仓内草稿目录**都要同样处理。本票的
   `.scratch/t25-*` 与 `pt-*` 均已被排除。
3. **`tui-grpc-spec.md` / `local-worker-bridge-spec.md` 的正文仍未订正**（只加横幅）。
   正文里可能还有更多「当时为真、现已假」的叙述（例如 `tui-grpc-spec.md` 的 4.x 各节细节）。
   本票**不**逐节重写：那是一次内容评审，需要每条重新取一手证据，应由独立票开。
4. **本机时钟与注入时间不一致**：本轮注入的 `current_time` 是 `2026-09-16 22:17`，
   而 `date` 实测 `2026-09-17 17:15 +0800`，且 HEAD 提交时间戳为 `2026-09-16 23:14 +0800`
   （T24 收口）⇒ 注入值**早于**已完成的提交，必为陈旧信号。任务目录/日期一律以 **`date` + git 时间戳**为准。
5. **⚠️ 判据 3 只挡住了「裸通配符」，没挡住「比它的理由更宽的模式」。** M4 暴露的风险有一半仍在：
   一条形如 `tui/**` 的 pattern 是合法的（有 `/`、非裸通配），但它同样可以覆盖整个 spec 的悬空集。
   本票**没有**加「规则不得覆盖该 spec 全部悬空提及」这条判据 —— 因为存在**合法**的全覆盖情形
   （`uc.scheduler.yaml` 恰好命中 scheduler-spec 的全部 4 条悬空提及）。
   ⇒ 真正的判据应是「模式宽度 vs 理由宽度」，而那是**语义**比对，非机械可判。
   **切片 C 若把提及升级为门禁，需要先决定这条怎么处理**（候选：要求每条规则在注释里声明它预期覆盖的
   路径清单，并对清单做逐条核对）。**本票只把「已实测到的那一半」堵上，并把剩余风险写在这里。**
