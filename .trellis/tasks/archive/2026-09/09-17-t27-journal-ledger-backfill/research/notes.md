# T27 / #674 研究与实测笔记

> 全部数字都来自本轮命令输出；凡「实测」字样背后都有一条可复算的命令。
> 本票的关键动作是**先量形状**，因为票面的清单与行号**都不全**（见 §2）。

## 0. 复算入口

```bash
# 全量普查：每个 session 的 6 个标准标题计数 + Testing 正文 + 三类占位符
python - <<'PY'
import pathlib, re
t = pathlib.Path(".trellis/workspace/Jamery Wang/journal-1.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
H = ["### Summary","### Main Changes","### Git Commits","### Testing","### Status","### Next Steps"]
P = ["- [OK] (Add test results)", "(Add details)", "- None - task complete"]
for s in P: print(len(re.findall(re.escape(s), t)), s)
b = [m.start() for m in re.finditer(r"(?m)^## Session ", t)]      # 按标题 POSITION 切片
for i, s in enumerate(b):
    e = b[i+1] if i+1 < len(b) else len(t)
    seg = t[s:e]
    print(re.match(r"## Session (\d+)", seg).group(1),
          {h: len(re.findall(r"(?m)^" + re.escape(h) + r"\s*$", seg)) for h in H})
PY

# 受控改动判据
git diff --numstat -- ".trellis/workspace/Jamery Wang/journal-1.md"
```

## 1. 为什么**不能**用票面给的那段复算脚本

票面的 `### 复算` 用 `re.split(r"(?m)^(## Session .*)$", t)` 切段，然后 `s.index("### Testing")`。
两个已知失效形态都会踩到：

1. **标题重复会让 re.split 错位**（T24 实测：Session 13 的重复标题让它把 18 处占位符数成 **19**）；
2. **某段真的没有 `### Testing` 时 `index()` 直接 ValueError** —— Session 13 的 stub 正是这种。

⇒ 本票一律用 `finditer` 的 **start() 位置**切片，并对「缺标题」显式分支（而不是崩掉）。

## 2. 三类之外的实测增量（票面低估了工作面）

```
placeholders: '- [OK] (Add test results)' x18   '(Add details)' x6   '- None - task complete' x19
'## Session' headings: 27   （唯一编号只有 26 个 ⇒ Session 13 出现两次）
duplicated headings INSIDE one segment:
  S12 @529  dup={'### Main Changes':2,'### Testing':2,'### Status':2}
  S13 @602  dup={'### Testing':2,'### Status':2}      <- 真块里也有
  S14 @714  dup={'### Main Changes':2,'### Testing':2,'### Status':2}
  S15 @837  dup={'### Main Changes':2,'### Testing':2,'### Status':2}
  S20 @1140 dup={'### Summary':2,'### Main Changes':2}
  S16/17/18 present=5/6 缺 '### Git Commits'
```

`18` 处 Testing 占位符 = **14 处（骨架型 session）+ 4 处（S12/S13/S14/S15 的骨架尾巴）**。
这个分解**必须先做**，否则会把「删掉尾巴」误当成「回填 14 处」。

## 3. 机制（结论 B 的一手证据）

S13 的 stub（590–601）：`## Session 13` + Date/Task/Branch + **`### Summary`（一句话浓缩版）** +
**空的 `### Main Changes`**，然后**紧跟**真正的一段（602 起）。

S14 的头部：

```
724 | ### Main Changes      <- 骨架的，空
726 | ### Main Changes      <- 手写正文重新起头
728 | Post-delivery verification of T12 (#654, affinity placement) ...
```

S14 的尾部（真内容都在这之上）：

```
807 | ### Status           <- 真的
809 | Complete. Commits `78c0d1d`, `7f69e62`, ... on main.
813 | ### Git Commits      <- 真的（哈希表）
824 | ### Testing          <- 骨架尾巴
826 | - [OK] (Add test results)
828 | ### Status           <- 骨架尾巴
830 | [OK] **Completed**
832 | ### Next Steps
834 | - None - task complete
```

⇒ **同一机制、三种外显**：空标题重复 / 占位尾巴并存 / 整块被取代。
修法必须按类处置，且**真内容（尤其 `### Git Commits` 的哈希表）一律保留**。

S20 的头尾同理，但它的重复是 `### Summary` + `### Main Changes`：
**两段 Summary 的文字都是真内容**（1148 短版 / 1154 长版），所以只删**冗余的标题**，两段文字都留下。

## 4. 回填的证据来源（每条都在文内标注）

14 个 session 的门禁数字**本来就在账本里**，只是不在 `### Testing` 段：

| session | 证据所在 | 关键数字 |
|---|---|---|
| 1 | `### Summary` | PR #627 的 **15 项 CI 检查全绿**（无本地逐项） |
| 2 | `### Summary` | Rust 428/370/180+8/35；clippy clean；pytest 977 |
| 3 | `### Summary` | 435/377/182+8/36/28；**7 个真 PG 实跑** |
| 4 | `### Summary` | 437/379/187+8/36/34；**17 个真 PG `--ignored` 实跑** |
| 5 | `## Gates` | 437/379/192+8/36/34；pytest 978+4；**并注明未跑什么** |
| 6 | `## 门禁终值` + `## 验收对照` | 437+5/379+5/192+8/36/35；pytest 977+4 |
| 7 | `## 门禁终值` | TS 162/17 files；pytest 970+11=977+4 |
| 8 | `## 质量门禁` | 452+5/379+5/195+8；pytest 988+5；TS 156/16 |
| 9 | `### Summary` | 439/381/204+8/36/35；pytest 997+5 |
| 10 | `### Summary` | types 40(+5)；pytest 1009+5 |
| 11 | `### Summary` | types 43(+3)；uc-grpc 206(+2)；pytest 1016+5 |
| 19 | `## 测试` + `## 门禁` | lib 446；types 47；grpc 238；pytest 1123/10；CI `f5ef707` 8/8 |
| 20 | `## 测试：五个形状…` + `## 门禁` | lib 447；真 PG 20 passed/11.49s；CI `1e61318`/`c6dfd20` 8/8 |
| 21 | `## 消融` + `## 门禁与一次账目更正` | lib 447→452；uc-grpc 238→240；types 47→50；pytest 1123→1139；CI `8a0645d` |

**Session 1 是唯一没有本地数字的** ⇒ 据实写「当时未记录」，**不臆造**。
6 个 `(Add details)`（1/2/3/9/10/11）的正文里确实没有明细 ⇒ 只给归档任务目录的指针。

## 5. `- None - task complete` 逐条复核（19 个）

判据两条，都**不是发明**：① 本段正文里**明写**的 `## Next` / `## 状态` 陈述；
② 否则用**下一个 session 的标题所指票据**（那是本账本的下一节）。

```
明写型：S5 `## Next`（T5→T6）/ S6 `## 状态`（T6 就绪）/ S12 `### Status`（P1 剩余 T12）
        S13 `### Status`+`### Known Limitations`（PG/NATS 欠账五笔）/ S15 `### Status`（#655 待关）
        S22 `## 残余`（竞态窗口未消除）
指针型：其余 13 个用「见 Session N+1」
```

⚠️ `- None - task complete` 的**语义本身是坏的**：它把「脚本没填」与「确实没有后续」呈现成同一句话。
本票**不用**第二种表述替换它 —— 一律写成**有据可查的后续**或「当时未记录」。

## 6. 手术的可审计性

- 每个删除区间都**逐字节断言**它要删的内容（`DEL` 表），一条不符就**不写盘**；
- 每个替换行都断言**原内容恰好是哪个占位符**（`EXPECT_REP`）；
- 断言「删除集 ∩ 替换集 = ∅」；
- ⚠️ **嵌入 `\n` 的替换值必须先转成 `\r\n`** —— 文件是 CRLF，直接写 `\n` 会留**孤立 LF**
  （T24 在 SKILL.md 上真踩过：4 处孤立 LF，且**断言是在文件已被写坏之后**才响）。
  本票把这一步**前移到写盘之前**，并加了一条 `assert "\n" not in new_text.replace("\r\n","")`；
- 写盘后**回读比对 + 行尾复核**。

## 7. 手尾（本票不做，已记账）

1. **Session 16/17/18 缺 `### Git Commits`**：17 的正文只出现过 `0c2604d`、18 是 `dd3d2b3`、
   16 一个自己的哈希都没有 ⇒ 补表 = 猜「哪些提交属于该 session」，**不做**。
2. **`journal-2.md` 里 index.md 的 `~257` 与实测 268 行不符**（index 的行数是**写入时**的快照，
   之后的手工编辑不回流）—— 属显示层的近似值，非欠账。
3. `add_session.py` 的骨架**仍在**产出这三个占位符 ⇒ **下一个 session 起还会再长出来**。
   本票只清存量；要治本得改脚本或加一个收口检查（**建议**：把它并入 `ci-scripts.yml` 的
   advisory 检查，或加进收口清单）。
