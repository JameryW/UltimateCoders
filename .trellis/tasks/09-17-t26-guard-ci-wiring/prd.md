# T26 — #675 切片 C：给豁免表加判据 4，并让 `scripts/**` 真正进入 CI

## 元信息

- 票号：T26（父票 **#675 切片 C**）
- 日期：2026-09-17
- 前置：T25 切片 B（`868cc92` 实现 / `d96f071` 归档 / `392e517`+`5c2bc00` journal / `2ad869b` 补提交清单）
- 交付面：`scripts/check-spec-refs.py`（判据 4 + 修一处已变成假话的注释）、
  `tests/python/test_check_spec_refs.py`（新）、`.github/workflows/ci-scripts.yml`（新）
- 不交付：spec 正文的任何改动、任何 `path:line` 引用的改动

## 背景

T23 让守卫**看见**了「无行号路径提及」这一类（advisory、恒不失败）；T25 把实测的 47 处**逐条分类并处置**
（7 处真过期改写 + 40 处带理由豁免），留下 `mentions: 40 of 227`、`0 unclassified`、exit 0。
但两件事被显式推给了切片 C：

1. **判据 3 挡不住「非裸但宽于理由」的 pattern**。T25 的消融 M4 实测：把一条规则从
   `new_module/impl.rs` 放宽成 `*`，两条既有判据**都不响**，`--audit` 照旧打印 `0 unclassified`
   —— 一次看起来完全健康的输出。T25 只补了「不得是裸通配符」，那挡不住 `ghost.*` 这类。
2. **`scripts/**` 在零个 workflow 的 `paths` 里**（T25 一手程序化读过四个 workflow）。守卫的任何回归
   CI 都看不见；而把测试放进 `tests/python/` **补不上**这个洞 —— 改 `scripts/**` 不触发它。

## 侦察结论（全部一手，逐条可复算）

### 结论 A：判据 3 的洞**可以机械封死**，因为豁免是「按条」的

一手测量每条规则的实际命中：规则侧 `{4, 1, 1, 1, 2, 2} = 11`，`SUBJECT_REMOVED` 侧 `27 + 2 = 29`，
**11 + 29 = 40 = 悬空总数**。⇒ 给每条 `MENTION_EXEMPT` 规则加一个**声明的命中数**并在自检里对账，
就能把「pattern 被放宽」变成红 —— 包括 `ghost.*` 这种判据 3 看不见的形状。

### 结论 B：`SUBJECT_REMOVED` **不该**带计数（有意的不对称）

它的作用域按设计就是**整个文件**，所以「命中几个」在那里不是安全性质；防止它静默的是判据 2
（spec 正文必须自认删除提交）。两条表的元数因此不同 `(spec, pattern, hits, reason)` vs `(commit, reason)`，
这是**自证的**不对称，不是疏漏。

### 结论 C：判据 3 与判据 4 **互相独立**（消融实测，非推理）

| 突变 | 期望 | 实测 |
|---|---|---|
| M0 基线 | 全干净 | `problems=0` |
| M1 把 `new_module/impl.rs` 的 1 改成 3 | 只红判据 4 | wildcard=0 / drifted=1 ✅ |
| M2 `tui/src/**` → `*`（该 spec 恰好只有 2 条悬空，**命中数不变**） | 只红判据 3 | wildcard=1 / drifted=0 ✅ |
| M3 `index.json` → `*.md`（**非裸**、但吞掉 record-session.md 的 2 条） | 只红判据 4 | wildcard=0 / drifted=1 ✅ |
| M4 `record-session.md` 声明数改成 9 | 只红判据 4 | wildcard=0 / drifted=1 ✅ |
| M5 端到端（`scripts/` 下临时副本，改计数） | 打印 + **exit 仍 0** | `self-check found 1` / `exit=0` ✅ |

⇒ 判据 3 与 4 打红的集合**不相交**（T19 判据：同一集合 ⇒ 其中一条是装饰），且 advisory 语义未变。

### 结论 D：Python 3.9 不构成阻塞 —— 一个**被证伪的假设**

我原以为守卫里的 `str | None` / `tuple[tuple[str, str, str], ...]` 会在 CI 的 3.9 矩阵下炸。
实测三点全过：① `from __future__ import annotations` 在**第 78 行**（函数标注、返回标注、模块级
`AnnAssign` 全部延迟求值）；② `ast.parse(src, feature_version=(3,9))` → OK；③ 用 AST 剔掉
`arg.annotation` / `returns` / `AnnAssign.annotation` 后重扫，**零**运行时 PEP604/585 用法（13 函数 0 命中）。
⇒ **不要把 3.9 当挡箭牌**；反过来，本票把 3.9 放进新 workflow 的矩阵，让这条兼容性主张**有 CI 背书**。

### 结论 E：接 `ruff` 有现成地雷

`ruff check scripts/check-spec-refs.py` 干净；`ruff check scripts/` **今天就是红的**（`I001` + `UP045`，
都在 `scripts/check-codex-issue-flow.py`，末次改动 `fc9b5ce`，属既有）⇒ 新 workflow 只能**点名文件**。
`ruff format --check` 改动前后都失败（T25 用 `git show HEAD:` 取证）⇒ **不接 format**。

## 变更（Scope）

1. **守卫加判据 4**：`MENTION_EXEMPT` 元数 `(spec, pattern, expected_hits, reason)`；
   `exemption_self_check()` 在判据 1 的 `elif` 分支上报「`declares N mention(s) but matches M`」。
2. **修一处已变成假话的注释**：同一个注释块写着 *"Two invariants … Both are checked"*，紧接着列了**三条**
   （T25 加判据 3 时写歪的）⇒ 改成四条并落本票的两个裁决。
3. **新增 `tests/python/test_check_spec_refs.py`（10 条）**：合成语料上钉判据 1/2/3/4 的**机制**，
   加两条跑**真实语料**的（表一致性 / `unclassified == 0`）。
4. **新增 `.github/workflows/ci-scripts.yml`**：`ruff check`（两个点名文件）+ 跑守卫 + 跑上面的测试；
   **无 `paths` 过滤**；矩阵 `["3.9", "3.12"]`。

## 裁决（本票必须落的两个决定，均已落）

### 裁决 1：mentions 的**计数**维持 advisory；被强制执行的是**表的一致性**与**仓库自证**

- 守卫的 exit code **不变**（判据 1–4 全部 advisory）：这是它的设计性质 —— **a mention is not a reference**，
  让「散文里提到一个文件」去红 CI 会产生假红。M5 端到端实测 `exit=0`。
- 被强制执行的部分落在**测试**里：表一致性（`exemption_self_check(real) == []`）与
  **`unclassified == 0`**。后者是**仓库自己的主张**，不是工具的性质 —— 测试 docstring 里写明了这一点。
  代价：新出现一条悬空提及会红，而修法就是加一条带理由的规则（一行）。这是**有意的摩擦**。

### 裁决 2：CI 接线取「**无 `paths` 过滤**」，不取「`paths: scripts/**`」

理由（一手）：守卫用 `os.walk(ROOT)` 建索引 ——**它读的是整个仓**，不只是 spec 目录。所以
`paths: scripts/**` 在语义上是错的：`crates/**`/`python/**`/`packages/**` 的**增删**同样能翻转判据
（加一个文件可能让一条豁免规则变成死规则；删一个文件会产生新的悬空提及）⇒ 带过滤就会**恰好对翻判词的那类改动瞎**。
代价：本 job 每次 push 都跑，所以它被刻意做得很小（不建 Rust、不跑整套 Python 套件）；
⚠️ **首次运行的实际耗时本轮未测**（无法在本地跑 runner），以第一次 CI 输出为准。
副作用（有意）：`scripts/**` 与 `.trellis/spec/**` 的提交**从此会触发一个 job**，T21–T25 那种
「收口提交零触发」的记录从 T26 起不再成立 —— 这是本票想要的。

## 验收

1. 守卫判据 4 落地，且**真实语料下** `exemption_self_check(collect()) == []`（表与语料同步）。
2. 判据 4 **会红**：M1/M3/M4 三种突变各自打红它；且与判据 3 的集合**不相交**（M2）。
3. 6 条规则的声明命中数与实测一致（`4+1+1+1+2+2 = 11`），与 `SUBJECT_REMOVED` 侧 `29` 合计 `40`。
4. 新测试 **10 条全绿**，且**突变自检**成立：拆掉判据 4 → 恰好 2 条红；拆掉判据 3 → 恰好 1 条红；
   恢复后守卫 sha256 按字节一致。
5. 守卫的对外数字与 exit code **一条不变**：`89 ok / 1 stale / 8 ambiguous / 0 structural`、
   `mentions: 40 of 227`（40 exempt / 0 unclassified）、`exit 0`。
6. `ruff check` 对守卫与新测试文件干净；两个文件 3.9 语法通过（`ast.parse(feature_version=(3,9))`）。
7. Python 收集总数对账：**1149 → 1159**（+10，恰为新测试条数）。
8. 新 workflow YAML 可解析、结构与其余四个一致；推送后**实测它被触发且为绿**（首次耗时记账）。

## 非目标

- **不改** spec 正文、不改任何引用（切片 A/B 已收口）。
- **不**把悬空提及升级为 exit code 失败（见裁决 1）。
- **不**修 `scripts/check-codex-issue-flow.py` 的两处既有 ruff 问题（不在本票）。
- **不**接 `ruff format`（会引入上百行无关重排）。
- **不**订正 `tui-grpc-spec.md` / `local-worker-bridge-spec.md` 的正文（那是内容评审，需逐节取一手证据）。

## 遗留（不在本票）

- 上述两篇已删子系统的 spec 正文仍只有横幅，未逐节订正 —— 应另开票。
- #674（journal 账本欠账）与本票无关，保持 open。
- `SUBJECT_REMOVED` 侧无计数：若将来该表增长，可考虑给它加「悬空数不得下降」之类的判据；
  现状（判据 2 的「必须还有悬空」）已覆盖「规则变成死规则」这一侧。
