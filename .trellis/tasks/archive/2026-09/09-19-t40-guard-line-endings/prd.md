# T40 —— 把「行尾一致性」从逐票手工实测升为全仓守卫

## 一、缺口（一手读数，HEAD `9624968`）

**5 个已跟踪文件的工作树内容行尾自相矛盾**（同文件内既有 CRLF 又有 lone LF），逐字节复算：

| 文件 | 字节 | 行数 | CRLF | lone LF |
|---|---|---|---|---|
| `dashboard/index.html` | 406 | 13 | 12 | **1** |
| `dashboard/src/grpc/engine_pb.ts` | 114266 | 3634 | 102 | **3532** |
| `packages/uc-orchestrator/src/grpc/engine_pb.ts` | 114286 | 3634 | 102 | **3532** |
| `tests/python/test_dashboard_metrics.py` | 26068 | 631 | 566 | **65** |
| `tests/python/test_worker_capabilities.py` | 11120 | 249 | 230 | **19** |

合计 **7149** 处 lone LF。`git ls-files --eol` 的 worktree 列里 `w/mixed` **恰好这 5 个**。

### 为什么现在没人发现（两条，都实测过）

1. **git 的内容层根本看不见行尾。** `core.autocrlf=true`（来源 = 沙箱自带 PortableGit 的
   **系统级** gitconfig `.../PortableGit/versions/1.2.0/etc/gitconfig`，**不是项目选择**）在
   比较时把工作树的 CRLF 抹成 LF ⇒ 混合文件与纯 LF 文件对 git 是**同一个内容**。
   `git diff` / `git diff --cached` 对这类差异**恒为空**（本票实测：5 个文件 `--numstat` 全空）。
   而 `git status` 在同一份字节上会给出**两种互相矛盾、且都没有信息量**的信号：
   未触碰 ⇒ 干净（stat 缓存命中）；刚写过 ⇒ ` M`（stat 过期）而 `git diff` 仍为空。
   ⇒ **判据必须在内容层（字节），不能是 `git status`。**
2. **仓库里已经有这条判据，但作用域窄、且不是判词。** `scripts/check-journal-ledger.py:427`：

   ```python
   # --- advisories: printed, never a verdict -----------------------------
   for s in stats_all:
       if s["lone_lf"]:
           print(f"ADVISORY: {s['path']} mixes line endings "
                 f"({s['lone_lf']} lone LF vs {s['crlf']} CRLF)")
   ```

   `read_journal()` 已经**同时返回 `crlf` 与 `lone_lf`**，检测逻辑一字不缺；缺的是**作用域**
   （只跑 journal）与**判词资格**（printed, never a verdict）。
   ⇒ 本票的缺口是**作用域 + 武装**，不是知识。

### ⚠️ 「git status 干净」不是内容判据（本票推翻了自己的假设）

修之前 5 个文件是 `w/mixed`，而 `git status --porcelain` **干净**；我把它们规范成 LF 之后，
`git status` 反而报 ` M`，`git diff` 却**依旧为空**。逐层实测得到的真相：

| 观测 | 事实 |
|---|---|
| `git diff --numstat` / `git diff` | **恒空**（内容层看不到行尾差异） |
| `git diff-files --raw` | 目标 sha 为**全零**（stat 脏标记） |
| `git update-index --refresh` | 5 个文件 `needs update` |
| **`git add` 之后** | **staged 为空**、`git status` 变干净、index blob 仍是 394 B / LF |

⇒ ` M` 是 **stat 缓存过期**造成的假信号，不是内容差异；`git add` 重算并记录 stat 即消失。
**推论**：① 「工作树干净」的判据必须是 `git diff --cached`/`git diff`（内容），不是
`git status --porcelain`（可能只反映 stat）；② 这 5 个文件在 **index 里本自始就是 LF**
⇒ 本票修的是**工作树残留**，**提交里不含它们**（`git diff` 空即判据），
验证方式是守卫转绿 + 与 index 逐字节相同（5/5 实测 `identical=True`）。

### 判据的形状：index 列 vs worktree 列（决定它在 CI 里能不能红）

| 列 | 数据源 | 判词可复现性 | 今天 |
|---|---|---|---|
| **index** | `git cat-file --batch` 喂 `:<path>`（索引语法，实测可用） | **跨平台逐字相同**（索引字节处处一致） | 干净（1820 个 `i/lf`，无 `i/mixed`） |
| **worktree** | 直接读工作树字节 | **依赖本机 checkout**（本沙箱 CRLF；CI Linux 全 LF） | 5 个红 |

**判据只判「是否混」，不判「用哪种」** —— 判「用哪种」会变成环境相关的判词（本机 CRLF、
CI LF），必然误报；判「是否混」在两处都成立：CI 里所有文件本就统一，绿得**正确**；
本机残留物一律红。

⚠️ **诚实交代**：J3（worktree）在 CI 里**今天不会红**（CI 的 checkout 全 LF）。
它仍然不是装饰 —— 一个混合 blob 一旦被提交（非 `autocrlf` 环境），CI 的 checkout **就是混合的**，
J3 会红。它的即时价值在本机：今天就有 5 个红灯（本票的真实漂移取证）。

## 二、为什么这不是装饰（前车之鉴，必须交代）

`scripts/check-tasks-refs-selftest.py:86-96` 记着：**一条 CRLF 突变被尝试两次、两次移除** ——
一次只让守卫崩溃（崩溃能打红测试但钉不住轴），一次改了什么都不变（`line.strip()` 本就吃掉 CR）。
结论是那个守卫「EOL-robust by construction，没有东西可钉」。

**本票与它不同，并且必须自证不同**：这里的性质**可证伪** —— 5 个文件今天就违反它，判词可复算、
可逐条消融。**撤回条款**：若消融发现新守卫既抓不到真实红灯、也抓不到任何合成红灯，
本票应**自行撤回**而不是交付装饰。

## 三、判据（各带独立失败消息 ⇒ 可逐条消融钉住）

| # | 判据 | 失败消息 |
|---|---|---|
| J1 | **非空性**：扫到的已跟踪文件数 ≥ `MIN_FILES`，且被判为二进制而跳过的数量 > 0 | `scanned 0 tracked file(s)` / `no binary file was skipped -- the classifier is broken` |
| J2 | **index blob 不混行尾** | `{path}: index blob mixes line endings ({crlf} CRLF, {lone} lone LF)` |
| J3 | **工作树不混行尾** | `{path}: working tree mixes line endings ({crlf} CRLF, {lone} lone LF)` |

**索引源** = `git ls-files -z`（T34/T36 的判据：索引源必须与「别人 checkout 出来的东西」一致，
`os.walk` 会看见本机被 ignore 的文件 ⇒ 同一提交两个判词）。因为走索引，
`.workbuddy/`、`tmp/`、`target/` 之类**天然不出现** ⇒ **不需要任何黑名单**（T36 的教训：
能换索引源就别加黑名单）。
**二进制判定** = 字节流含 NUL（自足、可测，不依赖 git 的 `-text` 推断）。

## 四、交付形状

- `scripts/check-line-endings.py`（LF）：判据 J1/J2/J3，各带独立消息；根目录由
  `__file__/../..` 推导 ⇒ 测试可整目录沙箱化，生产代码无 test-only 钩子。
- `tests/python/test_check_line_endings.py`（LF）：真仓 pin + 沙箱单点突变（每个判据至少一条）
  + **与 git 自身判词的跨实现对账**（`git ls-files --eol` 的 `i/` 与 `w/` 列）——
  ⚠️ 断言必须「**一致 + 非空**」（只断言一致 ⇒ 两个恒 False 的实现也算一致，T39 ⑦）。
- **接线进 `ci-scripts.yml` 的一个新 job**（**不加 `paths`**）：本守卫读的是**全仓**
  （`git ls-files`），按 T35 的判据，带 `paths` 过滤只会**蒙住**它；放进既有无过滤 workflow
  同时**不新增第 11 套 workflow** ⇒ 不移动 T37 守卫的计数词与表格行（本票因此**不动 README**）。
- **修掉那 5 个文件**：统一为 **LF**（= index 内容，也与「`tests/python/*` 是 LF-only」的既有记载一致）。
  ⚠️ 这 5 个文件**不出现在提交里**（index 本自始就是 LF，`git diff` 空）—— 修的是**工作树残留**。
  验证方式：守卫转绿 + 工作树与 index 逐字节相同（实测 5/5 `identical=True`）+ 不能靠
  `git status`（见上节的 stat 缓存陷阱）。
- `tests/python/test_check_tasks_refs.py`：语料钉值 `(794,795)` → **`(795,796)`**。

## 五、消融设计

| # | 形式 | 期望 |
|---|---|---|
| R | **真实漂移**（不在 pytest 里 —— 修之前的工作树已经不存在了） | 修前 `rc 1` = **5 个红灯**，逐字点名那 5 个文件，计数与手工复算逐字一致（12/1、102/3532、102/3532、566/65、230/19） |
| A | 沙箱里把工作树副本改成混合行尾（**不重新 `git add`**） | J3（工作树）红；且**不得**报 J2（index）——两条判据必须互不相交 |
| B | 沙箱里用 `git update-index --add --cacheinfo` **绕过 filter** 把混合 blob 塞进 index | J2（index）红（证明 J2 能失败，不是恒绿）；且**不得**报 J3 |
| C | 沙箱里删掉那个二进制 fixture | J1 的「跳过了 0 个二进制」红 |
| D | 沙箱里给一个**空 index**（只 `git init`，不 `add`） | J1 的「扫到 0 个文件」红 |
| F | **阴性对照**（不是突变）：`git status --porcelain` 对混合文件报干净 | 记录为「`git status` 不是内容判据」的第一手证据 |

**与实现的对应关系（实测，非计划）**：上表 A/B/C/D 就是
`test_every_judgment_is_pinned_by_a_mutation` 里的四段，逐条已跑通。
原计划把 C 写成「删掉 NUL 探测」（即注入一个代码突变），实现时改成**删掉那个二进制 fixture**：
两者都打红同一条消息，但删 fixture 不需要改动被测代码、也不受「同一消息被两条突变打红」的
干扰，是更干净的消融。**这是计划被实现修正的一处，按推论 A 在此同步。**

⚠️ 沙箱 fixture 必须**含一个二进制文件**（否则 J1 的「跳过了 0 个二进制」会误红）——
这是本守卫与测试之间一条**有意的耦合**，写在测试注释里。
⚠️ 沙箱 fixture 还必须含一个 **gitlink**（`git update-index --add --cacheinfo 160000,...`），
否则 `tracked_files()` 的 gitlink 分支无人覆盖。

## 六、验收（逐条可复算）

| # | 判据 | 终态读数（实测） |
|---|---|---|
| 1 | 修之前 rc 1 且逐字点名那 5 个文件；修之后 rc 0 | ✅ 修前 **rc 1，恰好 5 条具名失败**（12/1、102/3532、102/3532、566/65、230/19，与手算逐字一致）；修后 **rc 0** |
| 2 | 修之后打印扫到的文件数与跳过的二进制数 | ✅ `tracked file(s): N, text scanned: M, binary skipped: 9, gitlink(s) skipped: 1`。**两个可达态都实测**：实现提交 **1836/1827**（任务目录未跟踪）、归档后 **1840/1831** ⇒ 测试钉的就是这两个 |
| 3 | 5 个文件逐字节为 LF-only（`CR == 0`），且与各自 index blob 逐字节相同（5/5）；不出现在任何 diff 里 | ✅ `CR == 0` ×5；与 index blob 逐字节相同 **5/5**；`git diff` 里**一个都没有**（它们的 index 从一开始就是 LF） |
| 4 | 三条判据各有独立失败消息；每条至少一条突变能打红（覆盖断言强制） | ✅ 5 条消息；四条突变（A 工作树 / B index blob / C 无二进制 / D 空 index）+ 覆盖断言；A **不得**报 J2、B **不得**报 J3 ⇒ 打红集合不相交 |
| 5 | 与 `git ls-files --eol` 的跨实现对账通过（一致 **且** 非空） | ✅ 一致，且表内**同时含 `True` 与 `False`**（`assert verdicts == {True, False}`）；沙箱断言 `gitlinks == 1` |
| 6 | job 落在 `ci-scripts.yml`；该 workflow 无 `paths`；既有 9 套 workflow 的 `paths` 一字未改；不新增 workflow | ✅ `jobs` 从 3 → 4（`line-endings` 两腿）；`on` 无 `paths`（YAML 解析确认）；`git diff -- .github/workflows/` 只有 `ci-scripts.yml`、只有新增；workflow 计数仍 **10** |
| 7 | 语料钉值 `(795,796)`；`test_check_tasks_refs.py` 通过 | ✅ 两个可达态都实测：未跟踪 **795 ok**、归档后 **796 ok / 0 dangling / 0 malformed**；该文件 **14 passed** |
| 8 | Python 总收集数 = 1221 + 本票新增用例数；本地跑满全套 | ✅ **1226 = 1221 + 5**（逐字吻合）；全套 **1215 passed + 10 skipped + 1 failed**，那 1 条是沙箱 `safe-delete` 每轮批量计数触发（该文件单独跑 27/27 通过） |
| 9 | CI 上 `ci-scripts.yml` 全 job success，且逐字判词与本地一致 | ✅ Scripts CI **7/7**；两腿逐字 `1836/1827 … binary skipped: 9, gitlink(s) skipped: 1` + `line endings check passed.` + `5 passed` + ruff `All checks passed!`（3.9 腿跑通 ⇒ 兼容性前提仍成立） |
| 10 | （新增，实测出的）本票不移动 README 的任何计数 | ✅ 只加 job 不加 workflow ⇒ T37 的 README 表格与计数词不动，本票**未碰 README**；`check-readme-ci-table` rc 0 |

## 七、非目标与账（未静默丢弃）

- ❌ **不加 `.gitattributes`**：它把「checkout 用哪种行尾」升为全仓策略、影响下次 clone 的 1600+
  个文件，需要独立证据与决策；且本沙箱的 `autocrlf=true` 是**环境注入**的，加属性文件会让
  环境差异从「显式」变「静默」。⇒ 记**账 6**。
- ❌ 不动 `ruff format --check`（另有 **78** 个文件，大票）＝ **账 2**。
- ❌ 不动 `.claude/hooks/**` / `.trellis/scripts/**` 的 lint 排除（`ci-scripts.yml:37` 已明写故意排除）。
- ❌ 不把 journal 里那条 advisory 删掉：它按**文件**粒度报告 journal 的统计，与本守卫的
  「全仓是否混」是两条正交的判据；本票只要求它们不互相矛盾。
- 账 1（`docker compose` 挂载闭包）、账 4（`add_session.py` 个人 index 行数取自填充前）延续；
  账 5（README `Checks` 列散文）延续。

## 八、实现期新发现（两处，均已当场处置；判据都是「跑出来的」）

### 1. 新产物移动了**另一个测试文件**的钉值（T39 那条教训重演）

- 在本 workflow 里加了 3 个 `run:` 步骤之后，`tests/python/test_check_workflow_inputs.py`
  钉的真仓摘要 `18 run-step reference(s)` 变成 **20** —— 增量 2 而不是 4，因为那条计数是
  **去重后**的引用集合，本票新增的恰好是 2 个新文件（`scripts/check-line-endings.py`、
  `tests/python/test_check_line_endings.py`）。
- 该值由 10 套 workflow 文件的**内容**决定，与本票新增多少**跟踪文件**无关 ⇒ 它是**单值**、
  不是 T40 语料那种「可达对」，所以在同一个 change 里直接改成 20。
- 判据：跑那三个受影响测试文件时它**当场变红**（`assert 'workflows: 10 ... 18 ...' in out`），
  不是我推出来的 —— 与 T39「README 计数词移动了另一个测试文件的突变锚点」同形。

### 2. 任务目录的提交时机，决定 `implement.jsonl` 该写哪个路径

- 实测 T39：`prd.md` / `task.json` / `implement.jsonl` / `check.jsonl` **四个文件全部加在
  归档提交**（`d3a6103`），实现提交里**一个都没有**（逐个 `git log --diff-filter=A` 查证）。
- `check-tasks-refs` 的语料是**已跟踪**集合，且它校验 `.trellis` 引用指向的目标**必须已跟踪**。
  实测：只暂存 `implement.jsonl` + `check.jsonl` 时，第 1 行那处 `prd.md` 引用当场判
  **DANGLING**（`795 ok / 1 dangling`）；把 `prd.md` 一起暂存后才 `796 ok / 0 dangling`。
- ⇒ 两条结论同时成立：**实现提交里任务目录整体保持未跟踪**（这样语料停在 795，符合
  「实现提交不含任务文件」的既有事实），而 `implement.jsonl` **必须从一开始就写归档后的路径**
  （`archive/2026-09/...`），否则 `task.py archive` 一移动目录，引用立刻悬挂。
- 另：顺带实测到一个**会被误读的中间态** —— 任务目录只暂存一半时会得到
  `tracked file(s): 1839, text scanned: 1830`，**这个值 CI 永远不会看到**（任务文件是整批
  在归档提交里落地的），所以行尾守卫的计数钉值仍然只有两个可达态 `(1836,1827)` 与 `(1840,1831)`。
