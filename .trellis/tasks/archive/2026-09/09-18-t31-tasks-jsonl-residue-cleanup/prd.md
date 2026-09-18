# T31 —— 清理 `.trellis/tasks` jsonl 存量欠账

承接 **#681**（由 T30/#680 守卫映出）。守卫：`scripts/check-tasks-refs.py`。

## 1. 起点与判据

T30 交付守卫后，CI 与本地三方一致的起始态：

```
scanned 788 `.trellis` reference(s) in task context files
indexed 1777 tracked path(s)
summary: 774 ok / 14 dangling / 47 malformed
task context reference audit FAILED.   (exit 1)
```

**目标**：`exit 0`（`0 dangling / 0 malformed`），且 `Scripts CI` 的 `tasks-refs` job 转绿。
**守卫无白名单** ⇒ 只能**真修**。

## 2. 侦察结论（一手取证，非沿用旧记录）

### 2.1 `47 malformed` **不是一种形状，是两种**

按「逐行 `json.loads` 是否失败 + 能否单删行尾逗号修好」分类，**47 条全部归入两类，0 条 unclassified**：

| 类 | 条数 | 文件数 | 形状 | 修法 |
|---|---|---|---|---|
| **A：JSON 数组包装** | **20** | 2 | 首行 `[`，中间是 `{...},` 行，末行 `]`；键是 `"path"` 不是 `file` | 拆包成 JSONL |
| **B：行尾多余逗号** | **27** | 4 | 每行本身是合法 JSON 对象，但**行尾多一个 `,`** | 删末尾 `,` |
| 合计 | **47** | 6 | | |

**A 类分布**：
- `.trellis/tasks/archive/2026-06/06-24-multi-repo-config/check.jsonl`（7 条）
- `.trellis/tasks/archive/2026-06/06-24-multi-repo-config/implement.jsonl`（13 条）

**B 类分布**：
- `archive/2026-09/09-14-t9-merge-barrier/implement.jsonl`（8 条）
- `archive/2026-09/09-14-t10-context-compiler/implement.jsonl`（7 条）
- `archive/2026-09/09-14-t11-env-allowlist/implement.jsonl`（4 条）
- `archive/2026-09/09-15-t12-affinity-placement/implement.jsonl`（8 条）

⚠️ **B 类的取证细节**：`09-14-t9-merge-barrier/implement.jsonl` 是 **LF 行尾**，
第 1–8 行**各自以 `"},` 结尾**、第 9 行以 `"}` 结尾（干净）。
⇒ 判据是「**行尾多一个逗号**」，不是「整文件是数组」。
两类**修法不同**，必须分开处理。

### 2.2 `14 dangling` **也不是一种形状**

| 成因 | 条数 | 处置 |
|---|---|---|
| **C1：任务目录归档搬移后引用未改** | 4 | 指向 `archive/` 下的**同名文件**可直改 |
| **C2：路径少写了归档日期段** | 1 | `archive/2026-06/06-22-rust-scheduler/prd.md` 实为 `.../06-22-rust-scheduler/06-22-rust-scheduler/prd.md`（**嵌套一层同名目录**） |
| **C3：目标从未存在过（`spec/backend.md`）** | 1 | 该文件**不在 `git ls-files`**；`spec/` 下**只有 `backend/` 子目录**，无 `backend.md` |
| **C4：目标从未存在过（`spec/backend/workspace-config-spec.md`）** | 2 | 全仓**零命中**（`spec/backend/` 无此文件） |
| **C5：目标在 `spec/frontend/` 而非 `backend/`** | 2 | `type-safety.md` 实际在 `.trellis/spec/frontend/type-safety.md` |
| **C6：引用的是「未来的」未归档任务目录** | 4 | 指向 `.trellis/tasks/06-27-deep-analysis-of-omp-session-interruption-causes/...`；该任务已归档到 `archive/2026-06/` ⇒ **同 C1 性质** |
| 合计 | **14** | |

⚠️ **票面已警告：2 条是新形态、机械回填不安全。** 本轮定位到的是 **C3/C4（3 条，目标从未存在）** 与
**C5（2 条，目录名写错）** —— 这两类**不能靠「搬移」修**，必须**改写成正确路径**，
且必须先确认正确目标确实存在。

### 2.3 两类缺陷互不掩盖（复核 T30 的结论）

A 类的键是 `"path"`（**不是 `file`**），守卫只认 `file` ⇒ A 类**修好后不会新引入引用**。
B 类里确有 `file` 键，但**实测 0 条悬空**。⇒ **「14」不是少算的**，修 malformed **不会**改变 dangling 计数。

## 3. 实施步骤

1. **B 类（27 条）**：删行尾逗号。**保持原行尾风格**（逐文件实测），只改那一个字节。
2. **A 类（20 条）**：拆成 JSONL —— 去掉首 `[`、尾 `]`、每行尾逗号；**键 `path` 是否改名 `file` 需判定**
   （见 §4 的裁决）。
3. **C1/C2/C6（9 条）**：把引用改成**实际存在**的路径（逐条判定，**按边界感知匹配**）。
4. **C3/C4/C5（5 条）**：先确认真实目标；**确无对应文件者，删掉该条引用**（**不臆造路径**）。
5. 更新 `tests/python/test_check_tasks_refs.py::test_real_corpus_reproduces_the_recorded_numbers`
   到新数字（**这是有意钉住的，必须显式改**）。
6. 四道门禁 + 直落 main + 归档 + journal + 关 #681。

## 4. 待裁决（实施前定）

- **A 类拆包后，`"path"` 要不要改成 `"file"`？**
  理由两端：① 改成 `file` ⇒ 这 20 条进入守卫的引用计数（**会引入新引用，可能带来新的 dangling**）；
  ② 不改 ⇒ 这 20 条**永远不被守卫检查**（等于它们的历史信息留在「盲区」）。
  ⇒ 倾向 **不改**（**保持原样、最小改动**，且避免制造新欠账）；但**必须记录该裁决与理由**。
- **C3/C4 的 3 条「目标从未存在」**：**删掉引用**，不新建空文件（**不臆造**）。

## 5. 验收

1. `python scripts/check-tasks-refs.py` ⇒ **exit 0**，`0 dangling / 0 malformed`。
2. `Scripts CI` 的 `tasks-refs` job **转绿**。
3. 不得引入新的 dangling（守卫自身即判据，改完重跑）。
4. `test_real_corpus_reproduces_the_recorded_numbers` 已同步到新数字。
5. Python CI 收集数**只减不增**（本票只改数据文件，不加测试）。
