# T28 research notes — 一手量到的东西

## 0. 复算入口（都是只读；`$TEMP` 脚本不随票入库）

```bash
# 语料普查：每个 session 的 6 个标准标题出现几次 + 占位符严格/宽松双口径
python - <<'PY'
import pathlib, re
for f in ("journal-1.md", "journal-2.md"):
    t = pathlib.Path(".trellis/workspace/Jamery Wang/" + f).read_bytes().decode("utf-8")
    L = t.replace("\r\n", "\n").split("\n")
    print(f, {m: sum(1 for x in L if x.strip() == m)          # 严格
              for m in ["- [OK] (Add test results)", "- None - task complete",
                        "(Add details)", "(Add summary)"]},
          "| loose:", {m: sum(1 for x in L if m in x) for m in
                       ["- [OK] (Add test results)", "- None - task complete", "(Add details)"]})
PY

# 全语料 + pin 复核（就是守卫自己）
python scripts/check-journal-ledger.py --verbose

# session 是被哪次提交记录的（-S 数出现次数变化）
git log --format='%h|%ad|%s' --date=short -S "## Session 16: " -- ".trellis/workspace/Jamery Wang/journal-1.md"
```

## 1. 语料形状（2026-09-17 实测）

| 文件 | 字节 | 行 | session | 占位符（严格） | 6 标题齐全 |
|---|---|---|---|---|---|
| `Jamery Wang/journal-1.md` | 158,409 | 1,975 | 26 | 0 | 26/26 |
| `Jamery Wang/journal-2.md` | 42,251 | 381 | 4 | 0 | 4/4 |
| `JameryW/journal-1.md` | — | — | 56 | **165** | 56/56 |
| `JameryW/journal-2.md` | — | — | 57 | **168** | **56/57** |
| `JameryW/journal-3.md` | — | — | 10 | **30** | 10/10 |

- **票面说「两份 journal」，实际 5 份 / 2 本账**（`git ls-files '.trellis/workspace/**'` 证实 9 个被跟踪文件，
  两本账都是**已入库**的 ⇒ 干净检出也能看见 ⇒ 判词一致）。
- **票面说三个 session 的日期是 2026-09-16，实际是 2026-09-15**（三段的 `**Date**` 字段）；
  按日期回溯会拉错窗口，改用 `git log -S` 直接定位「记录该 session 的提交」。
- `JameryW/journal-2.md` 的 1 个非齐 session 与 3 处重复编号（49 ×2、74 ×2、98 ×3）**未处置**（另一本账）。

## 2. 判据口径：子串 vs 整行（同一份干净账本）

`journal-2.md`（记录 #674 本身的那份 —— 它**在正文里引用这三个占位符**）：

| 判据 | `- [OK] (Add test results)` | `- None - task complete` | `(Add details)` |
|---|---|---|---|
| 宽松（子串） | 1 | 3 | 4 |
| **严格（整行相等）** | **0** | **0** | **0** |

⇒ 子串判据在**干净**账本上就报红。这是 #676 要求「判据必须是整行相等」的一手依据，
也是 `tests/python/test_check_journal_ledger.py` 里 `test_substring_*` 钉住的那条。

## 3. 表内条目的口径：25/25 都不列入「记录自己那条提交」

对 journal-1.md 里**已有表格**的 25 个 session 逐个跑
`git log --format=%h -S "<该 session 标题>"` 找「引入标题的提交」，再与表内哈希比对：

```
S 1 tbl=['12b8cf6']                       intro=027c900 (chore: record journal)          intro_in_table=False
S13 tbl=['b3aa299']                       intro=12525bb (chore: record journal)          intro_in_table=False
S24 tbl=['d6dbba4']                       intro=15d0d26 (close out T21 — …)             intro_in_table=False
S26 tbl=['848b1c7']                       intro=b430f20 (close out T23 — …)             intro_in_table=False
…（25 个 session 全部 intro_in_table=False，无一例外）
```

⇒ 口径 = **该 session 的交付提交中、在「记录它的那条提交」之前落地的那些**。
机制解释：表格写于记录 session 的那一刻，写不进还不存在的提交。
（对照：S15 表内只有 `5bae604`，而记录它的 `689d87c` 之后还有 `f159018` / `d2bfadd` —— 都不在表内。）

回填时对每条都做了机械断言（`git merge-base --is-ancestor <表内提交> <记录提交>` = 0）+
提交信息片段匹配，任一条不符**不写盘**。

## 4. 守卫索引源：为什么是 `git ls-files`

- `.trellis/.gitignore` 第 2 行 `.developer` ⇒ **「当前开发者」是本机状态**，
  干净检出里 `get_developer()` → `None` ⇒ **无法在 CI 里算出「这本账」**。
- 本机 `os.walk` 会看见未被跟踪的文件（本地新建的开发者目录 / 本地实验）⇒ **同一提交两个判词**。
  T25 的 `40 exempt` 就是这个性质（本机有被忽略的 `.codex/config.toml`），T26 为 `check-spec-refs.py` 修掉了它。
- ⇒ 语料 = git 跟踪的 journal（`.trellis/workspace/**` 下 `journal-*.md`），其余用 pin 表处理。
- 钉住这一点的是 `test_untracked_journal_is_invisible_to_the_index`：往**真工作树**里丢一份
  含裸占位符的未跟踪 journal，断言**仍然 exit 0** —— 只有索引来自 git 才可能成立。

## 5. 消融自检（9 条非等价突变全部打红，集合两两不同）

守卫 `19364B / sha256 6fc139243f16c2fd`；每条突变：植入（断言锚点唯一）→ 跑 27 条测试 → **按字节复原 → 校 sha256**。

| 突变 | 打红 |
|---|---|
| M1 判据：整行相等 → 子串 | 5（prose / substring-pin / real-corpus / real-substring / untracked） |
| M2 去掉「无 session 的 journal」的 EMPTY_CORPUS | 1 |
| M3 索引：`git ls-files` → 文件系统走查 | 1 |
| M4 `strip()` → `strip(" ")` | **0 —— 等价突变**（见下） |
| M5a `main` 不再调用骨架自检 | 1 |
| M5b 骨架自检恒通过 | 1 |
| M6 不查重复 session 编号 | 1 |
| M7 不比对 legacy pin | 1 |
| M8 抑制 HEADING_COUNT | 4 |
| M9 `read_journal` 不再归一化 CRLF | 1 |

- **M4 是等价突变**：`read_journal` **先**把 `\r\n` 归一化成 `\n`，所以任何比较都见不到 `\r` ⇒
  单点改比较函数不可能影响 CRLF 行为。这条不是「没被钉住」，是「改不动行为」。
  为此把 `test_crlf_journal_is_still_measured` 的断言下沉到**读入器本身**
  （`read_journal` 的输出里不得含 `\r`），于是 M9 能打红它。
- 三处**重叠**（同一测试被多条突变打红）都发生在 `test_real_corpus_conforms` /
  `test_untracked_*`：它们跑的是**真语料**，任何改变真语料判词的突变都会（正当地）打红它们。
  没有任何两条突变的**集合完全相同** ⇒ 无装饰突变。
- **一处必须记一笔的意外**：第一次消融跑完后，守卫里残留了 M8 的 `if False:`（`+2` 字节，就是 M8 的突变体），
  而消融脚本自己的「复原 + 断言」当时是**通过**的。是**另起一次调用复算 sha256** 才发现的
  （`6fc139243f16c2fd` → `376faa1601891bec`）。教训：**复原脚本的自断言是自指检查，不是独立证据**；
  消融后必须由**另一个进程**（或下一次工具调用）复算哈希。手工还原那一行后哈希精确回到 `6fc139243f16c2fd`
  ⇒ 证明残留只有那一行，没有别的损伤。

## 6. 回填手术的可审计性

- 三处插入点由「会话切段 → 段内唯一的 `### Testing` 标题」定位（**不是**全局匹配 `### Testing`，
  它在文件里出现几十次）；插入前断言前一行为空、段内尚无 `### Git Commits`。
- 3 个哈希 × 3 条断言（存在 / 是记录提交的祖先 / 提交信息片段相符）在**构建输出之前**跑完。
- 候选文本在**写盘之前**就通过了整份文件的终检：26 个 session 全部 6 标题各一次、占位符 0。
- 写盘后回读逐字节相等 + 行尾复核；`git diff --numstat` = **24/0**（3 × 8 行，纯新增，无删除）。
- 1280×? 无关：全文行尾仍是 CRLF（1974 CR / 0 孤立 LF）。

## 7. 未处置 / 交给后续

1. **另一本账 `.trellis/workspace/JameryW/`**：363 处占位符 + 3 处重复 session 编号（49/74/98），
   已被 pin 住（不会变坏）但未清理；其 `index.md` 写着 `Total Sessions: 119` 而语料有 123 个标题。
   #676 的范围是交付流程写的那本账 ⇒ 建议**另开票**。
2. `journal-2.md` 的 `index.md` 行数快照（`~257` vs 实测 381）—— 显示层近似，不重算，不记欠账。
3. 塔尖未纳入门禁的：行尾统一（只作 ADVISORY，因为它随 `core.autocrlf` 在 CI/本机不同）。
