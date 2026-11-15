# 05 — AST 优先的符号定位 (T05)

**What to build:** 把「改了哪个符号」从行号升级为稳定的 dotted symbol（Python），
其它语言回退行号；永不抛异常，作为 P3 恢复视图与 P4 知识页的共同键。

**Blocked by:** D3（符号定位规则）
**Spec:** [spec-p1.md](../spec-p1.md)
**Status:** implemented（`python/ultimate_coders/knowledge/attempt_chain.py::
locate_symbols` 及 `_symbol_nodes` / `_innermost_nodes` / `_hunk_ranges`；
focused cases in `tests/python/test_attempt_chain.py`）

**Implementation**

- `locate_symbols(path, diff, *, source_text="", max_symbols=20) -> list[SymbolChange]`：
  - `.py` + 有源码：`ast.parse()` 遍历 class/def 得到 dotted 名与 `(start,end)`；
    用 hunk 变更范围取**最内层**节点（`_innermost_nodes`）；`kind="function"` /
    `"class"` / `"symbol"` 视节点类型。
  - 无源码 / 非 `.py`：按 hunk 区间产出 `kind="line"` 的 `SymbolChange`。
  - 任何 `SyntaxError` / AST 异常都被吞掉并返回空列表。
- 每轮最多 `MAX_ROUND_SYMBOLS = 32` 个符号；worker 侧源码读取上限 500 KB
  （`worker.py::_worktree_source`）。

**Acceptance gates**

- [x] 单函数内改动 → 命中该函数（不是整个文件）。
- [x] 嵌套 class/def → 命中 innermost，dotted 名正确。
- [x] 非 Python 文件 / 源码缺失 / 语法错误 → 回退行号区间且不抛异常。
- [x] `max_symbols` 截断生效。

**Rollout:** 随采集开关（≥B）；纯函数，无 IO 副作用。
**Risks:** symbol 名对后续轮次的行号漂移免疫，但**不免疫重命名**——重命名后同一
符号会以新名出现，需要 P3 视图按 path 兜底；JS/TS 暂只有行号级定位。
