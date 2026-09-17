# T25 — #675 切片 B：47 处悬空路径提及的分类与处置

## 元信息

- **Status**: in_progress
- **Implements**: #675（**切片 B**；切片 A 已随 T23 交付）
- **Depends on**: T23（守卫看得见「无行号提及」）、T24（#673 切片 B 剥行号，同族口径）
- **Related**: #673（切片 C 的 CI 接线与本票的门禁化是同一类决策）、#674（journal 欠账，无关）
- **Scope 面**: `.trellis/spec/**` 9 篇 spec + `scripts/check-spec-refs.py`
- **非目标**: **不改 exit code**、不接 CI、不提交合成语料 pytest（三条都属切片 C）

## 背景

`scripts/check-spec-refs.py` 原先只扫 `path:line`。同一个路径**不写行号就完全不可见**，而
「文件已经不存在」与行号无关 —— 实测 **47 处**提及指向不存在的文件，守卫一个字都看不到。

切片 A（T23，`848b1c7`）已让守卫**看见**这一类：新增 `mention` 类（反引号包裹 + 路径形状 +
围栏外 + 不含 `:行号`），恒 advisory、不改 exit code。本切片负责**逐条分类并处置**，
并为「合法不存在」的那些提供**带理由的豁免标记**，使切片 C 的门禁决策变成机械可判。

## 侦察结论（全部为一手证据，逐条可复算）

### 结论 A：47 处里**没有一处**能靠「同名文件换路径」修好

34 个不同 `(spec, ref)` 的 **basename 在本仓任何深度都不存在**
（复用守卫自己的 `_repo_index()` 复核，34/34 均为 0 候选）。
⇒ 「改指向新位置」只有在**换一个文件名**时才可能，**必须逐条判定，不能套公式**。

### 结论 B：票面对 `tui/**` 的定性是**错的** —— 不是「另一个仓库」，是**在本仓被删除**

issue #675 与守卫 docstring 都写「`tui/**` 27 处描述的是**另一个仓库**的项目」，据此把它归入
「仓外 ⇒ 合法豁免」。实测**推翻**：

| 证据 | 命令 | 结果 |
|---|---|---|
| `tui/` 曾被跟踪 | `git log --diff-filter=A -- "tui/*"` | 有多个 `feat(tui):` 提交 |
| `tui/` 已不在 HEAD | `git ls-tree -r --name-only HEAD \| grep '^tui/'` | 空 |
| 删除提交及其理由 | `git log --diff-filter=D -1 -- tui/src/reducer.ts` | **`d7f4631`** (2026-06-25) *"feat(orchestrator): replace TUI frontend with OMP extension (#157)"*，正文逐字写着 **"Delete tui/ directory (Ink/React TUI no longer needed)"**，84 files / **15336 deletions** |
| 替代物仍在仓内 | `git ls-tree -r --name-only HEAD \| grep '^packages/uc-orchestrator/src/'` | `extension.ts`、`ui/*`、`orchestrator/events.ts` 等在 |
| **spec 没被同步修改** | `git show d7f4631 --name-only \| grep -i tui-grpc` | **空**；`tui-grpc-spec.md` 最后改于 **2026-06-23**（`b897a7d`），**早于**删除 |

⇒ 定性从「仓外项目（合法豁免）」改为「**在本仓被删除、spec 未同步**（真过期）」，处置也随之从
「豁免」改为「**加状态横幅**」—— 两者都对外宣称「这 27 处不是缺陷」，但只有后者**基于事实**。
**这正是「间接信号 ≠ 一手事实」的又一次实例：票面写下的定性自带权威感，一手证据就在 `git log` 里。**

### 结论 C：47 处塌缩成 7 类，且每一类都有可点名的删除/迁移提交

| # | 类 | 行数 | 锚点 | 一手证据 | 处置 |
|---|---|---|---|---|---|
| 1 | **已删子系统：TUI** | **29** | `tui-grpc-spec.md` 27 + `taskservice-grpc-spec.md` 2 | `d7f4631`（见结论 B）；替代物 `packages/uc-orchestrator/src/**` 在仓内 | spec 加**状态横幅**（2 篇）+ 小节注解（1 篇） |
| 2 | **已删子系统：local-worker JSON-RPC bridge** | 2 | `crates/uc-grpc/src/local_worker.rs`、`python/ultimate_coders/local_worker.py` | **`a368371`** (2026-06-27) *"fix(bridge): replace hand-rolled JSON/gRPC with connectrpc gRPC-Web client (#171)"*，两文件同一提交删除 | 同上：整篇 spec 加横幅 |
| 3 | **运行时/运维提供** | 4 | `uc.scheduler.yaml` | spec 自述：`Gateway loads uc.scheduler.yaml at boot (UC_SCHEDULER_CONFIG env → ./uc.scheduler.yaml). **Missing file = idle scheduler** (opt-in)`（`scheduler-spec.md:329`，另见失败表 `:435/:436`）⇒ 仓里本就不该有 | **带理由的豁免** |
| 4 | **举例 / 命名规范示例** | 5 | `new_module/impl.rs`（"Add implementation files as needed (e.g., …)"）、`rate_limiter.py`（命名规范表 Example 列）、`index.json`（"checking if `index.json` exists" 的通用探针例）、`record-session.md` ×2（Trellis 跨平台命令模板） | 均出现在**举例语境**，非引用 | **带理由的豁免** |
| 5 | **目录表列了已删文件** | 1 | `backend/directory-structure.md:72` 的 `sandbox/` 行含 `docker.rs` | **`514ec3b`** (2026-07-12) *"chore(sandbox): remove dead DockerSandbox and UC_SANDBOX_MODE knob (#237)"*；实测 `crates/uc-engine/src/sandbox/` = `{agents/, file_tracker.rs, mod.rs, pool.rs, subprocess.rs}` | **改 spec**（该表是**当前**目录清单，必须为真） |
| 6 | **测试文件指针已删** | 4 | `tests/python/test_agent.py` ×2（backend/frontend `quality-guidelines.md`）、`test_nats_worker.py`、`test_dashboard.py` | **`47f2add`** (2026-06-26) *"chore: consolidate repo structure (round 2) (#163)"*，三文件均为 `D`（**非 rename**，该提交的 `R100` 只在 docker 文件上） | **改 spec**（指向真实的测试组织） |
| 7 | **Python → Rust 迁移** | 2 | `python/ultimate_coders/agent/scheduler.py` ×2（`scheduler-spec.md:100`、`dashboard-spec.md:116`） | **`15b5ae3`** (2026-08-04) *"chore: remove dead Python scheduler.py after Rust SchedulerService activation (#548)"*；继任者 `crates/uc-python/src/scheduler.rs` 首行自述 *"PySchedulerService — Python-facing Scheduler class"*，`lib.rs` 注册 `PySchedulerService` | **改 spec**（重指 uc-python） |
| | **合计** | **47** | | | |

**两轴正交且重叠**（#675 已更正过一次）：按 spec 文件 27/5/3/3/2/2/2/2/1，按路径前缀
`tui/` 12 + 裸名 26 + `tests/` 4 + `python/` 3 + `crates/` 1 + `new_module/` 1。
**「`tui/**` 27 处」只在按文件说时成立，按前缀说应是 12**（27 处里 15 处写作裸名）。

### 结论 D：修复后仍有 40 处「合法不存在」—— 需要一个带理由的豁免机制

7 类里 5 类是**真过期**（改 spec）、3 类是**合法不存在**（类 1/2/3/4，共 **40 行**）。
切片 A 只让守卫**看见**它们；若不把「合法」固化成机器可读的东西，切片 C 就没有可门禁的口径，
而且 40 处会变成一个人人学会忽略的常驻 advisory。
⇒ 本切片在守卫里加 `MENTION_EXEMPT`（`(spec, ref-glob, reason)` 规则）
与 `SUBJECT_REMOVED`（整篇 spec 级，**必须在 spec 正文里点名删除提交**才被承认）。

## 变更（Scope）

1. **分类** 47 处 → 7 类（结论 C 的表即交付物之一，逐条附一手命令）。
2. **改 spec（真过期 7 行）**：`directory-structure.md`（删冗余 `docker.rs`）、`backend/frontend
   quality-guidelines.md`（把假的「单文件 `test_agent.py`」改成真实的按组件分文件）、
   `nats-bridge-spec.md`（重指真实 NATS 测试文件）、`scheduler-spec.md` + `dashboard-spec.md`（重指 `crates/uc-python/src/scheduler.rs`）。
3. **加横幅/注解（已删子系统 31 行）**：`tui-grpc-spec.md`、`local-worker-bridge-spec.md` 加状态
   横幅（点名删除提交与替代物）；`taskservice-grpc-spec.md` 对 TUI 客户端小节加注解。
4. **守卫加豁免机制**：`MENTION_EXEMPT` + `SUBJECT_REMOVED`；输出把 dangling 拆成
   `exempt` / `unclassified`；新增自检（规则必须命中 ≥1 条、横幅必须点名提交）。**exit code 不变**。
5. **更正被证伪的叙述**：守卫 docstring 的「out-of-repo project」改为「removed subsystem」，
   并记录 T25 的分类结论。

## 验收

1. `python scripts/check-spec-refs.py --audit` **exit 0**，`structural == 0`，**`unclassified dangling == 0`**。
2. 修复前 47 → 修复后 40，且 **40 条全部有 reason**；`resolved + dangling + ambiguous == total` 三方对账。
3. **消融（一次一处突变）**：① 删掉某篇 spec 的横幅 → 自检报警；② 把一条豁免规则改成匹配不到的
   形状 → 自检报警（而非静默放过）；③ 新增一条**真**悬空提及 → 该条进 `unclassified`（证明豁免集
   不是「什么都吃」）；④ 恢复后逐字节等于突变前（校 sha256）。
4. 7 处修复**逐个**验证：修复后该 mention 要么解析到真实文件、要么不再被扫到（`docker.rs`）。
5. 9 篇被改 spec 的 `git diff --numstat` 均为小改动（删除列 ≪ 文件行数，无整文件重写）。
6. 守卫**既有**判定不变：`89 ok / 1 stale / 8 ambiguous / 0 structural` 与 T24 收口时一致。

## 非目标

- **不把提及升级为门禁、不接 CI** —— 切片 C 的决策面（`.trellis/**` 一旦进 CI 的 `paths` 就会
  开始跑 Python CI，是**触发面**变更，不在本票）。
- **不提交合成语料的 pytest** —— 切片 C 首项（见 #675 的「建议的切片」）。
- 不改 `path:line` 引用的既有判定与 exit code。
- 不重写 `tui-grpc-spec.md` / `local-worker-bridge-spec.md` 的正文（保留为历史记录，只加横幅）。

## 交付后回读（2026-09-17，收口时逐条对照）

**Status: completed**（实现 `868cc92`）。

| 验收 | 结果 | 证据 |
|---|---|---|
| 1 | PASS | `--audit` **exit 0**、`structural=0`、**`unclassified=0`** |
| 2 | PASS | **47 → 40**；`exempt=40 == dangling=40`；`166 resolved + 40 + 21 ambiguous = 227`（stdout 与 `--json` 两条路径一致） |
| 3 | PASS | 见下方「验收 3 的两处修正」 |
| 4 | PASS | `.scratch/t25-verify.py`：7 处逐条验证（死提及 `gone`、活指针 `resolves`、仍悬空 **0**） |
| 5 | PASS | 9 篇 spec 均为小改动（最大 `10/17`）；守卫 `198/16`；改动后孤立 LF 全 0 |
| 6 | PASS | `89 ok / 1 stale / 8 ambiguous / 0 structural`，与 T24 收口**逐项相同** |

### 验收 3 的两处修正

1. **原文写的四条突变里，M4 的期望是错的。** 原稿把 M4 写成「用来确认放宽的边界*可观测*，
   不预设结论」——**实测发现它不是「可观测」，而是「完全静默」**：把 `new_module/impl.rs`
   放宽成 `*` 后，注入的无关提及被一并豁免，而自检**一个问题都不报**，`--audit` 仍报
   `0 unclassified`。**这不是期望落空，是设计漏了判据** ⇒ 当场补**判据 3**（pattern 不得是裸通配符），
   补后 M4 报 `self_check_problems=1`。**原稿的措辞（「探索格」）低估了它**，故此处保留原文并写明修正。
2. **突变矩阵由四格扩到六格**：新增 **M4（裸通配符）** 作为独立一格（原 M4 的「探索」意图由它承接），
   并明确 **M5 = 恢复后与 M0 逐字节相同**（原稿已有，实测确认三个文件 sha256 相等）。

⇒ 教训与 T21/T22 同源：**「我以为它会报警」不能替代「跑一次看它报了没有」**；
且**补判据后必须重跑同一突变**，否则新判据本身没被验证过。

## 遗留（不在本票）

- **切片 C**：是否升级为门禁 / 是否接 CI / 是否加合成语料 pytest。**#675 因此保持 open。**
- **两篇已删子系统的 spec 正文未订正**（只加横幅）—— 逐节重写需每节重新取一手证据。
- **判据 3 的残余风险**：只挡裸通配符，不挡「比理由更宽的模式」（详见 `research/notes.md` §8 第 5 条）。
