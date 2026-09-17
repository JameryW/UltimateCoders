# T29 recon / 证据（#677）

所有数字都是**跑出来的**。复算脚本在 `%TEMP%`（不入库）：
`uc-t29-abl.py`（测试级消融，含对照突变）、`uc-t29-recon2.py`（`EXCLUDE_DIRS` 存活度 + MB 输出差）。

## §0 基线（一手）

```
guard : 39904 B, 纯 CRLF (786 CRLF / 0 孤立 LF), sha256 ddaadb29b048251a
tests : 11 passed  (tests/python/test_check_spec_refs.py)
guard : exit 0
        summary: 89 ok / 1 stale(advisory) / 8 ambiguous(advisory) / 0 structural failure(s)
        mentions: 41 of 227 resolve to no file (41 exempt by documented reason, 0 unclassified)
```

## §A 测试级消融（含对照突变）

命令：`python %TEMP%/uc-t29-abl.py`

```
=== MC CONTROL: disable the bare-wildcard invariant (KNOWN pin -- must RED)
    bytes delta -32   guard exit=0   guard output changed: False
    tests red: 1  -> ['tests/python/test_check_spec_refs.py::test_bare_wildcard_pattern_is_reported']
=== MB drop the identifier-exact filter on the bold anchor (undo T24 #1)
    bytes delta -33   guard exit=0   guard output changed: True
    tests red: 0  -> (none)
=== MS drop ".scratch" from EXCLUDE_DIRS (undo T24 #2)
    bytes delta -12   guard exit=0   guard output changed: False
    tests red: 0  -> (none)
[finally] guard restored; sha256 ddaadb29b048251a

--- verdicts ---
control red set: ['...::test_bare_wildcard_pattern_is_reported']  (harness proven to detect regressions)
UNPINNED MB -> 0 tests red, but CLI output changed
DEAD     MS -> 0 tests red AND no CLI change
control red set | others: disjoint
```

⚠️ 脚本对「DEAD」的措辞有误导：MC 也打印了同一句，但 MC 是**纯测试级**的钉
（该不变量只在 `MENTION_EXEMPT` 表有坏条目时才在 CLI 上发声，真语料里没有）。
⇒ **判词应由「打红数」决定，不由「CLI 是否变化」单独决定**；本票只把 MB/MS 当结论用。

### 突变构造（避免 T24 的 CRLF 陷阱）

全部用正则从**文件字节**定位后做纯删除/替换：

| 突变 | 锚（正则） | 替换 |
|---|---|---|
| MC | `pattern\.strip\(\) in \{"\*", "\*\*", "\*\.\*"\}` | `False` |
| MB | ` if IDENT_RE\.fullmatch\(b\.strip\(\)\)` | 删除 |
| MS | `"\.scratch", ` | 删除 |

每格：写盘 → 跑 11 条测试 → 跑守卫 CLI → **按字节恢复** → 复校 sha256（每格都校，不只最后）。

## §B MB 的确切输出差（一手）

命令：`python %TEMP%/uc-t29-recon2.py`

```
line 7:  ADVISORY: 1 references look symbol-stale   ->  2 references look symbol-stale
line 11: 89 ok / 1 stale / 8 ambiguous / 0 structural
      -> 88 ok / 2 stale / 8 ambiguous / 0 structural
total differing lines: 2
```

⇒ 与 T24 docstring 的 `STALE 27->26, OK 113->114` **同向同量**（该 ref 回到假 STALE）。
**子句在起作用，但 0 条测试会红** ⇒ 本票要补的就是这条钉。

## §C `EXCLUDE_DIRS` 存活度（一手）

`git ls-files -- <dir>`，再按 `CODE_EXT` 过滤（守卫第 300 行同一套后缀）：

| 条目 | 跟踪文件 | 其中代码文件 |
|---|---|---|
| `.git` | 0 | 0 |
| **`.scratch`** | **12** | **12** |
| `target` | 0 | 0 |
| `node_modules` | 0 | 0 |
| `vendor` | 1 | **0** |
| `.venv` | 0 | 0 |
| `__pycache__` | 0 | 0 |
| `dist` | 0 | 0 |
| `build` | 0 | 0 |
| `.pytest_cache` | 0 | 0 |

`.scratch` 的 12 个 basename：
`D4-nats-transport-convergence.md` / `D5-commit-barrier-ownership.md` / `D6-resume-semantics.md` /
`D7-upgrade-window-inflight-policy.md` / `map.md` / `T1.md`…`T7.md`

⇒ 在 git 路径（第 302 行）上只有 `.scratch` 有活输入，而它改变 **0 条判定**（§A 的 MS）。
其余 9 项只对 `_walk_index` 回退路径有意义。

## §D 为什么 T24 的理由过期了（机制，不是时间线巧合）

1. T24 时代索引 = `os.walk` ⇒ **看得见本机 `.scratch/`**（回滚副本 + `pt-test_*/**/lib.rs` 脚手架树）
   ⇒ 排除 `.scratch` 有可测效应（`lib.rs` 候选 4→79）。
2. T26 把索引换成 `git ls-files`（`_repo_index` 第 285–305 行）⇒ **未跟踪的 scratch 永远进不了索引**。
3. 剩下的唯一活输入是那 12 个**被跟踪**的 `.md`，而 basename 不撞任何提及 ⇒ 效应 0。
⇒ **同一条子句、同一份代码，判词从「改变 1 条判定」变成「改变 0 条」——变的是索引源，不是子句。**
（这与 T25 的 `40 exempt` 同型：**环境相关的读数，不能当作子句的固有性质**。）

## §E 手交 / 未处置

- **`.scratch` 保留与否**是决策，本票保留现状并记录两种理由（prd §6）。
- `EXCLUDE_DIRS` 的 9 项死条目：只写明，不重构。
- `_walk_index` 回退路径**没有测试**：它只在「非 git 检出」或 `git ls-files` 失败时走到，
  合成语料测试恰好走的是它（tmp ROOT 不是 git 仓库），所以**间接**被覆盖；
  但「回退路径与 git 路径给同一答案」这件事**没有**专门的钉 —— 记在此，未开票。
