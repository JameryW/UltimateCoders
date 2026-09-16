# T23 #675 研究记录 — 「无行号路径提及」盲区

全部数字可复算。复算命令见每节标题下的 `$` 行；本票的侦察脚本在 `.scratch/`（不入库）。

---

## §A 盲区的规模与形状规则

```bash
python scripts/check-spec-refs.py            # 落地后：mentions 计数 + 悬空数
python scripts/check-spec-refs.py --audit    # 悬空清单
```

**提及 = 反引号包裹 + 路径形状 + 无 `:行号` + 在代码围栏之外。** 实测：

| 解析结果 | 数 |
|---|---|
| 精确命中（写全路径） | 55 |
| 唯一 basename 命中 | 47 |
| basename 歧义 | 22 |
| **解析不到任何文件（DANGLING）** | **47** |
| 合计提及 | **171** |

**为什么必须排除代码围栏。** 围栏内的路径是**图示**而不是引用：实测围栏内另有 **73** 个路径形状 token
（`directory-structure.md` 用围栏画整棵树）。不排除就会成批进来。

**形状规则与 `PATH_SPAN_RE` 刻意不同**（后者答的是另一个问题：「这个 span 是不是路径，因此不能当内容锚」）：

| 维度 | `PATH_SPAN_RE`（内容锚用） | `MENTION_PATH_RE`（提及用） | 理由 |
|---|---|---|---|
| `.md` | **不含** | **含** | 跨 spec 链接（`worker-service-spec.md`）是可能失效的引用 |
| 前导 `.` / `/` | 允许 | **不允许** | `.mcp.json`、`./uc.scheduler.yaml`、`/app/docker/...` 是**运行时/容器侧**产物，不是仓内相对引用 |
| `:` | 允许（行号后缀） | **不允许** | 从形状上保证 `path:line` 永远不会同时被算成提及 |

> 这条分割是**测出来的**，不是设计的：首版直接用 `PATH_SPAN_RE`，与独立探针一对账就有 **15 处差异**
> （少 8 条 `.md`、多 7 条前导 `.`/`/`）。详见 §C。

---

## §B 47 处悬空提及的全量清单

47 处悬空提及。**先看两个正交的分布轴** —— 它们**不是**同一批的两种数法，会重叠：

按 **spec 文件**：

| spec | 悬空提及 |
|---|---|
| `tui-grpc-spec.md` | 27 |
| `scheduler-spec.md` | 5 |
| `directory-structure.md` | 3 |
| `cross-layer-thinking-guide.md` | 3 |
| `local-worker-bridge-spec.md` | 2 |
| `nats-bridge-spec.md` | 2 |
| `quality-guidelines.md` | 2 |
| `taskservice-grpc-spec.md` | 2 |
| `dashboard-spec.md` | 1 |

按**路径前缀**：

| 前缀 | 数 |
|---|---|
| （裸 basename） | 26 |
| tui | 12 |
| tests | 4 |
| python | 3 |
| new_module | 1 |
| crates | 1 |

> ⚠️ **两个轴会重叠，不可相加**：`tui-grpc-spec.md` 一篇贡献 **27** 处，但其中多数写成**裸 basename**（`reducer.ts`、`keymap.ts` …）⇒ 前缀轴只数出 **12** 个 `tui/`。因此「`tui/**` 27 处」这句话**只在按文件说时成立**，按前缀说应是 12 —— 本票最初就是这么写错的（见 §F 第 7 条）。

### 逐条清单（按 spec 文件分组）

**`cross-layer-thinking-guide.md`** — 3 处

| 行 | 路径 |
|---|---|
| 90 | `record-session.md` |
| 99 | `record-session.md` |
| 134 | `index.json` |

**`dashboard-spec.md`** — 1 处

| 行 | 路径 |
|---|---|
| 116 | `python/ultimate_coders/agent/scheduler.py` |

**`directory-structure.md`** — 3 处

| 行 | 路径 |
|---|---|
| 72 | `docker.rs` |
| 77 | `rate_limiter.py` |
| 127 | `new_module/impl.rs` |

**`local-worker-bridge-spec.md`** — 2 处

| 行 | 路径 |
|---|---|
| 17 | `crates/uc-grpc/src/local_worker.rs` |
| 47 | `python/ultimate_coders/local_worker.py` |

**`nats-bridge-spec.md`** — 2 处

| 行 | 路径 |
|---|---|
| 353 | `tests/python/test_nats_worker.py` |
| 353 | `tests/python/test_dashboard.py` |

**`quality-guidelines.md`** — 2 处

| 行 | 路径 |
|---|---|
| 9 | `tests/python/test_agent.py` |
| 148 | `tests/python/test_agent.py` |

**`scheduler-spec.md`** — 5 处

| 行 | 路径 |
|---|---|
| 100 | `python/ultimate_coders/agent/scheduler.py` |
| 327 | `uc.scheduler.yaml` |
| 329 | `uc.scheduler.yaml` |
| 435 | `uc.scheduler.yaml` |
| 436 | `uc.scheduler.yaml` |

**`taskservice-grpc-spec.md`** — 2 处

| 行 | 路径 |
|---|---|
| 60 | `tui/src/grpc/client.ts` |
| 308 | `tui/src/grpc/types.ts` |

**`tui-grpc-spec.md`** — 27 处

| 行 | 路径 |
|---|---|
| 17 | `tui/src/grpc/client.ts` |
| 32 | `tui/src/hooks/useGrpcClient.ts` |
| 57 | `tui/src/hooks/useTaskEvents.ts` |
| 118 | `tui/src/components/CjkTextInput.tsx` |
| 134 | `cjk-input-utils.ts` |
| 143 | `cjk-input-utils.ts` |
| 145 | `tui/src/cjk-input-utils.ts` |
| 165 | `tui/src/components/StatusBar.tsx` |
| 195 | `tui/src/reducer.ts` |
| 350 | `reducer.ts` |
| 351 | `keymap.ts` |
| 352 | `formatters.ts` |
| 353 | `symbols.ts` |
| 354 | `truncate.ts` |
| 355 | `filter.ts` |
| 356 | `cjk-input-utils.ts` |
| 357 | `chatlog-utils.ts` |
| 359 | `tui/vitest.config.ts` |
| 547 | `tui/src/symbols.ts` |
| 562 | `tui/src/keymap.ts` |
| 658 | `keymap.ts` |
| 670 | `reducer.ts` |
| 671 | `formatters.ts` |
| 672 | `symbols.ts` |
| 673 | `truncate.ts` |
| 674 | `filter.ts` |
| 675 | `cjk-input-utils.ts` |


---

## §C 两路复算：守卫 vs 独立探针

```bash
python .scratch/t23-reconcile.py   # 比对 (spec, line, ref) 键集与 verdict
```

首版守卫与独立探针（`.scratch/t23-mentions.py`）**不一致**：守卫 `170 mentions / 50 dangling`，
探针 `171 / 47`。差值拆开是 **+8 / −7**：

- **守卫少 8 条**：`PATH_SPAN_RE` 的扩展名表**没有 `md`** ⇒ `worker-service-spec.md`(×4)、
  `decomposer.md`、`type-safety.md`、`record-session.md`(×2) 全部被拒。
- **守卫多 7 条**：`PATH_SPAN_RE` 把首字符留给 `[` 的字符类允许了前导 `.`/`/` ⇒ `.mcp.json`(×2)、
  `.config.toml`(×2)、`./uc.scheduler.yaml`、`/app/docker/docker-compose.yml`、
  `../../crates/uc-grpc/proto/engine.proto` 被收进来，其中 5 条悬空。

⇒ 定下 `MENTION_PATH_RE` 后，**键集 169 / 169、双向差异 0、verdict 分歧 0**。
（169 是去重后的键数 —— 有两处同一行同路径出现两次；行数 171。）

⚠️ 顺带确认：**没有把 T22 的既有数字搅动** —— `148 refs / 113 ok / 27 stale / 8 ambiguous /
0 structural / 0 content-mismatch` 全部不变。提及走的是**新正则**、**新 verdict 名**，
且 `main()` 里 refs 与 mentions 分别切分后统计，所以既有的过滤器不可能误收提及。

---

## §D 消融（8/8）

```bash
python .scratch/t23-ablation.py    # 探针走真 CLI 子进程；每例后按字节恢复并校 sha256
```

基线：`refs=148 mentions=171 dangling=47 ambiguous=22 resolved=102 exit=0`（spec 文件 sha `f87d1f6a3e56e640`）

| 突变 | 实测 delta | 期望 | 结果 |
|---|---|---|---|
| M0 基线（不变） | `{}` | `{}` | PASS |
| M1 追加一条**悬空**提及 | `mentions +1, dangling +1`，**exit 仍 0** | 同 | PASS |
| M2 同一条放进**代码围栏** | `{}`（完全不计） | `{}` | PASS |
| M3 追加一条**存在**的提及 | `mentions +1, resolved +1` | 同 | PASS |
| M4 追加 `path:行号` | `refs +1`，**mentions 不变** | 同 | PASS |
| M5a 追加 `.md` 链接 | `mentions +1, resolved +1` | 同 | PASS |
| M5b 追加前导 `.` 路径（`.mcp.json`） | `{}` | `{}` | PASS |
| M6 追加**坏引用**（`no_such_file_zzz.rs:10`） | **exit 1**，refs +1，dangling 不变 | 同 | PASS |
| M7 恢复后复跑 | 与基线逐字段相等 | 同 | PASS |

**两条信息值得单独记：**

1. **M6 是本票的关键一条**：它证明新增的提及类**没有放松既有的引用门禁** ——
   坏引用照样 exit 1。否则「advisory 不提权」就成了空话。
2. **M1 与 M2 必须成对**：只做 M1 只能证明「提到就会报」，**证明不了围栏排除**；
   只做 M2 只能证明「围栏里不报」，**证明不了围栏外会报**。两条一起才钉住「围栏是唯一的分界」。

**一次自我纠错**：M5a 首跑报 FAIL，delta 是 `{mentions +1, resolved +1}` 而我期望写成 `{mentions +1}`
—— 是**我的期望值漏了一项**（`.md` 目标存在 ⇒ resolved 也 +1），不是代码问题。改期望后 PASS。
（与 T22 那次「探针把 heading 写到非行首」同型：**先怀疑自己的断言，再怀疑代码**。）

---

## §E 顺带修正 #673 的一个数字：可去行号 **84 → 52**

```bash
python .scratch/t23-classify.py    # 锚点分类 + 内容字面量在目标文件里的出现次数
```

#673（以及 T22 `notes.md`）写「**84 / 148 今天就能去行号**」（仅符号 25 + 仅内容 41 + 两者 18）。
那是**存在性**口径：本行有符号、或有内容匹配，就算可去。

加一道**唯一性**检验后结论变了 —— **内容锚里 32 / 41 的匹配字面量在目标文件里出现 >1 次**：

| 字面量 | 在目标文件里出现 | 引用 |
|---|---|---|
| `abort_on_failure` | **14** 次（`agent.rs`） | `agent-capability-spec.md:560` |
| `retry_count` | **15** 次 | `:561` |
| `parallel_group` | **20** 次 | `:564` |
| `retry_delay_ms` | 9 次 | `:562` |
| `agent_config_json` | 12 次 | `:559` |

⇒ 行号一去掉，读者（和守卫）都无法区分「指的是哪一处」。**唯一出现的内容锚只有 9 条。**
⇒ **安全可去行号 = 43（符号锚：两者 18 + 仅符号 25）+ 9（唯一内容锚）= 52**，不是 84。
余 96 = 32 条内容歧义 + 64 条无锚。

**这条修正改变了切片的顺序**，见 §F 第 1 条。

---

## §F 限制与已知边界（照实记，不包装）

1. **它把 #673 切片 B 从「可开工」变成「被阻塞」** —— 这不是意外，是**测出来的依赖**：
   重写会把 52 处引用变成无行号形态，正好落进本票补上的视野。若先重写、后补视野，
   那 52 处会在守卫里**静默消失**（`148 refs` 变 `96 refs`，且无任何提示）。
2. **提及只判解析，不判锚。** 本票**不做**「本行命名的符号是否仍定义在目标文件里」——
   那是切片 B/C 的事，且需要一个自己的消融（语义放宽要独立证明）。
3. **提及永远不是门禁**（advisory）。理由写在守卫 docstring 里：47 处含运行时配置、文档举例、
   命名规范示例、**仓外项目**（`tui/**` 27 处）。朴素门禁 = 当场 47 红、大多数非缺陷，
   与 T21 的「63 报 / 61 误报」同型。**「绿」现在仍然不等于「spec 里每个路径都存在」。**
4. **只扫反引号内**。裸写（无引号）的路径不视为提及 —— 否则散文里的每个文件名都会进来。
   代价：`ref_NOT_in_tick` 那 1 条（引用形态）不受影响（`REF_RE` 扫原文），
   但**裸写的提及**（如 `` `worker.py` `` 写成 worker.py）不计入。已记账，未处理。
5. **围栏判定是行级的**，不处理嵌套围栏或用 4 个反引号开的围栏。本仓 spec 未出现这两种写法（已实测）。
6. **`scripts/` 不在 CI 的 lint 面内**（`ci-python.yml` 只跑 `ruff check python/ tests/`）。
   本票改的文件 `ruff check` **通过**；`ruff format --check` 在**改动前**就不通过
   （已用 `git stash` 验证是既有状况），故**不做全文件重排**以免混入百行噪声。

7. **本票发布过两处被自己推翻的表述，已更正**：① issue #675 首版把 47 处写成「`tui/**` 27 + 裸 basename 26 + …」，那是把**按文件**与**按前缀**两个轴**相加**了 —— 实际是同一批的重叠视图（`tui-grpc-spec.md` 一篇 27 处，其中 15 处写作裸名）。② `prd.md` 同处相同错误。两处均已改为按文件为主轴、前缀为副轴并标注重叠。
