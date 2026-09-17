# T26 研究与实测笔记（#675 切片 C）

> 全部数字都来自本轮命令输出；凡「实测」字样背后都有一条可复算的命令。
> 逐节与本票 prd 的结论 A–E 对应，另加遗漏与自我更正。

## 0. 复算入口

```bash
# 守卫当前状态（含豁免表自检与 summary）
python scripts/check-spec-refs.py --audit

# 每条规则的实际命中（本票的 4/1/1/1/2/2）
python - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location('g', 'scripts/check-spec-refs.py')
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
rows = g.collect()
d = [r for r in rows if r['kind'] == 'mention' and r['verdict'] == g.DANGLING]
pref = g._SPEC_PREFIX
short = lambda r: r['spec'][len(pref):] if r['spec'].startswith(pref) else r['spec']
import fnmatch
for s, p, n, _ in g.MENTION_EXEMPT:
    print(s, p, 'declared=', n,
          'actual=', len([r for r in d if short(r) == s and fnmatch.fnmatch(r['ref'], p)]))
PY

# 新测试（本地；-o addopts="" 跳过 pyproject 的 coverage）
.venv/Scripts/python.exe -m pytest tests/python/test_check_spec_refs.py -o addopts="" -q

# 收集总数对账
.venv/Scripts/python.exe -m pytest tests/python/ --collect-only -q -o addopts="" | tail -1
```

## 1. 为什么「按条声明命中数」能把判据 3 的洞封死

判据 3（T25）问的是「pattern 是不是裸通配符」——**形状问题**，所以任何非裸 pattern 都溜过去。
判据 4 改问「这条规则命中了几条」——**数量问题**，而**数量是 pattern **宽度**的观测量**：
放宽 pattern ⇒ 覆盖集只会变大 ⇒ 除非该 spec 的悬空集恰好就那么几条，数量必然变。

⚠️ 那个「除非」是真实存在的：M2 实测（`tui/src/**` → `*`）数量**没变**（该 spec 恰好 2 条悬空），
所以判据 4 **不替代**判据 3，而是**互补**。两条都在，才是完整覆盖。

### 不对称是有意的

`SUBJECT_REMOVED` 不带计数。它的作用域按设计就是整个文件 ⇒「命中几个」在那里**不是安全性质**
（文件里所有路径都是历史性的，本来就该全豁免）。防止它静默的是判据 2：
`_subject_removed_reason()` 只有在 **spec 正文里出现该删除提交哈希**时才返回理由
—— 删掉横幅不是「打印个警告」，而是**该文件当场的豁免整体失效**。这条是 T25 设计里的关键，不是本票新增。

## 2. 消融：判据 3 与判据 4 互不替代（本轮实测）

```
== 内存突变矩阵（w=判据3裸通配 / d=判据4计数 / dead=判据1死规则）==
OK M0   problems=0  wildcard=0 drifted=0 dead=0
OK M1   problems=1  wildcard=0 drifted=1 dead=0   <- 计数写错（1 -> 3）
OK M2   problems=1  wildcard=1 drifted=0 dead=0   <- tui/src/** -> *，命中数不变
OK M3   problems=1  wildcard=0 drifted=1 dead=0   <- index.json -> *.md（非裸！）
OK M4   problems=1  wildcard=0 drifted=1 dead=0   <- 声明 9 条实际 2 条
== M5 端到端 ==  exit=0 / 输出含 'self-check found 1 problem(s)' / 含 'hit count drifted'
守卫 sha 未变: d25e8646b6f79c99
```

**M3 是关键那一格**：`ghost.*` / `*.md` 这类**非裸** pattern 判据 3 看不见，判据 4 直接打红。
这正是 T25 记为「残余风险」的那条。

### 我在这条上先写错过一次（留档）

首轮消融我把 M3 设成「`new_module/impl.rs` → `*`，**期望两条都红**」，实测**只红判据 3**。
原因不是缺陷：`backend/directory-structure.md` 恰好只有 **1** 条悬空提及，放宽成 `*` 命中数仍是 1
⇒ 判据 4 没有理由响。**是我把突变选错了**（它退化成了与 M2 同形）。
修订后新增 `index.json → *.md`，才真正落在「非裸但更宽」的形状上。

## 3. 测试的突变自检（新测试也要证明会红）

```
基线（未突变）:  10 passed in 2.70s   returncode=0
突变 A：拆掉判据 4  ->  2 failed, 8 passed
        failed: test_declared_count_must_match_the_corpus
                test_pattern_wider_than_its_reason_is_reported
突变 B：拆掉判据 3  ->  1 failed, 9 passed
        failed: test_bare_wildcard_pattern_is_reported
守卫 sha 恢复校验: d25e8646b6f79c99 == d25e8646b6f79c99  True
```

一次只拆一处、每次恢复后校 sha256、且**恰好**是预期那几条变红（没有额外红 —— 额外的红要能解释才算过）。

## 4. 测试怎么写才不撞坑（三条，都是本仓既有事实）

1. **文件名带连字符 ⇒ 不能 `import`**：`scripts/check-spec-refs.py` 必须用
   `importlib.util.spec_from_file_location` 加载。它**没有导入副作用**（扫描跑在
   `if __name__ == "__main__":`（:705）之后，`main()` 在 :583），所以 import 是安全的。
2. **`ROOT` / `SPEC_DIR` / `_SPEC_PREFIX` 是模块级常量**（:89 / :90 / :505），且被函数在**调用时**读
   ⇒ 合成语料测试必须 monkeypatch 这三个（外加 `_ACK_CACHE.clear()`，它按 spec 缓存横幅判定结果）。
3. **`Path.write_text(..., newline=...)` 是 Python 3.10+**，而 CI 矩阵含 3.9 ⇒ 合成语料一律用
   **`write_bytes`**：既避开 3.9 的 TypeError，也避开默认的换行翻译（会在每行尾留 `\r`，
   而守卫读的是 `read_bytes().decode()`，那会让 `\r` 变成行内容的一部分）。

另一条环境事实：本机跑 `tmp_path` 时**没有被沙箱拦**（本次 `10 passed in 3.17s`，无 setup error）——
记忆里那条「单批 >50 删除被拦」在本票的树规模（≤4 个文件）下不触发。

## 5. 一条最值得留的回归钉：两个提及判词只能靠 `verdict` 区分

`DANGLING` 与 `MENTION_AMBIGUOUS` **都有 `target is None`**。T25 我就是用 `not row["target"]` 过滤，
得到 **68**，与守卫自报的 **47** 不符；多出的 21 条是 ambiguous（同名文件有多个候选），不是 dangling。
`test_ambiguous_and_dangling_are_distinct_verdicts` 把这条钉住：断言两者 `target` 都是 None、
`candidates` 一空一非空、而**只有 dangling 才进悬空集**。

## 6. 触发面：为什么不带 `paths` 过滤

守卫的索引来自 `os.walk(ROOT)`（`_repo_index()`），**读的是整个仓**。所以：

- `paths: scripts/**` 只覆盖「改了脚本」，而**翻判词的往往不是脚本改动**：
  加一个 `crates/x/y.rs` 可能让一条豁免规则变成**死规则**（判据 1 红）；删一个文件会产生**新的悬空提及**
  （真实语料测试红）。
- ⇒ 带过滤等于**恰好对最需要看的那类改动瞎**。所以本 job 无过滤、每次 push 都跑，
  并把自身做小（不建 Rust、不跑整套 Python 套件、只 load 一个脚本）。

**代价与未测项**：每次 push 多 2 个短 job（矩阵 3.9 + 3.12）。**首次实际耗时本轮无法本地测**，
以第一次 CI 输出为准并记账。副作用有意：`scripts/**` 与 `.trellis/spec/**` 的提交从此有 CI。

## 7. 3.9 兼容性：一个被证伪的假设（留档）

我先把「3.9 会炸」当成了切片 C 的阻塞项，理由是守卫里到处是 PEP 604/585 标注。**实测三点全过**：

- `from __future__ import annotations` 在**第 78 行**；
- `ast.parse(src, feature_version=(3,9))` → OK（唯一的 `^\s*match\s` 命中是 `match = pattern.match(line)`
  这个**变量名**，假阳性）；
- AST 剔掉全部标注后重扫：**零**运行时 `list[...]` / `X | Y`（13 个函数，0 命中）。

⇒ 结论从「阻塞」改成「本票顺手把它钉住」：新 workflow 的矩阵含 3.9，兼容性主张从此有 CI 背书。
（同一族教训：**别把未验证的假设写成阻塞** —— 与 T25「间接信号当事实」第 6 条同源。）

## 8. 已知限制（诚实记账）

1. 判据 4 钉的是**数量**，不是**语义**。一条 pattern 若恰好宽度相同但理由不匹配（例如把
   `index.json` 换成另一个同样只命中 1 条的 pattern），计数对账看不出来。理由是**散文**，不是可校验字段 ——
   这是这类豁免机制的固有上限，不是本票能关掉的。
2. 真实语料两条测试**依赖仓库当前状态**：`unclassified == 0` 会在新增悬空提及时报红（**有意**），
   `exemption_self_check == []` 会在规则失配时报红（**有意**）。若将来确有正当的例外，修法是加规则。
3. `ci-scripts.yml` 的**首次耗时与首次绿**本轮未验证（需要 push 之后看 `gh run list`）。

## 9. CI 首跑打红：索引依赖本机被忽略文件（本票最重要的产出）

### 9.1 症状与逐字证据

推送 `6e748e8` 后 **Scripts CI（35207013920）与 Python CI（35207013916）同时红**。
Scripts CI 的 3.9 与 3.12 两档都在同一步失败，日志逐字：

```
tests/python/test_check_spec_refs.py::test_real_corpus_has_no_untriaged_dangling_mention FAILED
E   AssertionError: assert [('.trellis/s...config.toml')] == []
E     Left contains one more item: ('.trellis/spec/backend/agent-capability-spec.md', 380, 'config.toml')
```

**注意**：其余 9 条（含 3.9 档下 `from __future__ import annotations` 的兼容性）**全部 PASSED** ——
即我在本票事先证伪的「3.9 会炸」确实不成立，而真正的问题出在别处。日志还给出
`pythonLocation: /opt/hostedtoolcache/Python/3.9.25/x64`，确认矩阵生效。

### 9.2 根因

```
$ find . -name 'config.toml' -not -path './target/*' -not -path './vendor/*' -not -path './.git/*'
./.codex/config.toml
$ git check-ignore -v ./.codex/config.toml
.gitignore:90:.codex/	./.codex/config.toml
```

`_repo_index()` 用 `os.walk(ROOT)` 建索引 ⇒ **它索引本机文件系统，而不是这个仓库**。
本机有被忽略的 `.codex/config.toml`，CI 的干净检出没有 ⇒
`agent-capability-spec.md:380` 的 `` `config.toml` `` 在本机解析得到、在 CI 悬空。
**同一提交两种答案**，而 T25 的收口数字（`40 exempt / 0 unclassified`）是在**有那个文件的那一侧**测的。

一手规模测量：

```
os.walk 索引：488 个 basename / 1320 个路径
git ls-files：1764 个路径 -> 按 CODE_EXT 过滤后 468 个 basename / 1154 个路径
只在 os.walk 里出现的 basename：67 个   （.workbuddy/memory/**、.agents/**、.opencode/**、.reasonix/**、.codex/** ...）
只在 git 里出现的 basename：12 个       （.scratch/durable-runtime-migration/** —— 见 9.3）
```

### 9.3 修法的两种坏形态（都实测过，都否掉）

- **只往 `EXCLUDE_DIRS` 加 `.codex`**：打地鼠，不修「类」；而且下一个被忽略的目录还会重演。
- **纯 `git ls-files` 索引**：会把 12 个**在 `.scratch/` 被 ignore 之前就已提交**的文件放回来
  （`.scratch/durable-runtime-migration/{map.md,tickets/T1..T7.md,issues/D4..D7*.md}`，
  一旦被跟踪 `.gitignore` 就不再对其生效）⇒ 等于**撤销 T24 的决定**（`.scratch/pt-test_*/**/lib.rs`
  曾把 `lib.rs` 候选从 4 抬到 79）。实测 B \ C = 恰好这 12 个。

### 9.4 采用形态与其可测性

索引 = `git ls-files -z` **∩** `EXCLUDE_DIRS`（按路径分量过滤）∩ `CODE_EXT`；
`git` 不可用时退回 `_walk_index()`，且**只在 `ROOT/.git` 存在（即 git 本该可用）时才告警** ——
这样合成语料测试（tmp 目录、非 checkout）不会刷警告，而一个退化的真仓运行不会被误当成权威结果。

| 索引 | basename | 路径 | 与 A 的判词差异 |
|---|---|---|---|
| A 现状 | 488 | 1320 | — |
| B 纯 git | 468 | 1154 | — |
| **C git ∩ EXCLUDE_DIRS** | **456** | **1142** | **1 / 325** |

**C 下悬空 = 41**（规则侧 `4+1+1+1+2+2+1(config.toml) = 12`，整篇 `27+2 = 29`，12+29 = 41，0 未分类）。

### 9.5 被顺带钉住的两件事

1. **新增第 11 条测试** `test_repo_index_is_built_from_git_not_from_the_filesystem`：断言索引里
   没有 git 不认识的路径。⚠️ **诚实标注其不对称性**：它只在「本机确实有被忽略文件」时会红，
   CI 的干净检出即便带着这个 bug 也会通过（那里 walk == git）。**CI 侧看到的是 `unclassified == 0` 那条**。
   两条合起来才是完整覆盖，这一点写进了测试 docstring。
2. **T25 的数字被正名**：`40 exempt` 在本机成立、在干净检出是 41 —— 不是 T25 算错，而是
   **那个数字当时是环境相关的**。本票消掉了这个性质。

### 9.6 复算入口（本节新增）

```bash
# 索引来源与规模对比（A/B/C）
python .scratch/t26-index-probe2.py

# 三者对判词的影响：应恰好 1 行（agent-capability-spec.md:380 config.toml）
```

### 9.7 未测项（诚实记账）

修后**尚未推送**，因此「CI 转绿」这一步在本轮末尾才由推送后的 `gh run list` 背书；
另外 Scripts CI 的**实际耗时**（两档矩阵）本轮仍未本地测得。
