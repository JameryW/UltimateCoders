# 03 — 统一编译面与 worker 双路径 (T03)

**What to build:** 让「上游 gateway 组合块」与「本地依赖注入器」两条上下文来源
都只产出 `ContextItem`，经过同一个 `compile_context()` 与同一个预算；gateway
路径也落 manifest（旧行为：不编译、不留记录）。同时提供可插拔 token 计量接口。

**Blocked by:** 无（D2 已决定）；词汇来自 T01。
**Spec:** [spec-p0.md](../spec-p0.md)
**Status:** implemented（`python/ultimate_coders/agent/context_compiler.py`、
`python/ultimate_coders/agent/worker.py::_execute_subtask`；focused cases in
`tests/python/test_context_compiler.py`、`tests/python/test_context_block.py`、
`tests/python/test_context_outbox.py`）

**Implementation**

- 适配器：`from_dependency_results()`（P3 TRACE，`trust_class="worker_reported"`）、
  `from_gateway_context_block(block, *, source="gateway-context")`（读
  `{"entries":[{"node_id","success","summary"}],"truncated"}` → TRACE，
  `trust_class="gateway_committed"`）、`from_search_block(text)`（P2 EVIDENCE，
  `trust_class="repository_search"`）、`from_hy_memory_block(text)`（P4 MEMORY，
  `trust_class="hy_memory_recall"`）。
- token 计量：`DEFAULT_TOKEN_METER = "heuristic"`、`register_token_meter(name,
  meter)`、`resolve_token_meter(name=None) -> (key, meter)`（未知名字抛
  `ValueError`）、`ContextManifest.token_meter`（进 `summary()`）、
  `compile_context(..., token_meter=None)`。
- worker：`_execute_subtask()` 里 gateway 块 → `source_items` +
  `legacy_style="gateway"` + `gateway_truncated=block.get("truncated")`；否则
  依赖注入器 items + `legacy_style="worker"`；一次
  `compile_context(source_items + from_search_block(...) + from_hy_memory_block(...),
  budget_tokens=self._context_budget_tokens(), risk=self._last_route.risk)`；
  `manifest.render_legacy(legacy_style=..., gateway_truncated=...)`；
  两条路径都 `self._last_context_manifest = manifest.summary()` 并
  `_record_context_manifest()`。
- 旧 `_render_gateway_context_block()` 已删除（无死代码）。

**Acceptance gates**

- [x] 两条路径都经过 `compile_context`，gateway 路径也写 manifest
      （`tests/python/test_context_outbox.py`）。
- [x] `render_legacy` 与旧渲染逐字兼容，只在真正丢弃时追加截断标记
      （`tests/python/test_context_compiler.py`、`tests/python/test_context_block.py`）。
- [x] 适配器的 tier / trust_class / source 有断言；gateway 块截断标记透传。
- [x] token meter 注册表：未知名字抛错、默认 `heuristic`、manifest 记录本次 meter。
- [x] `UC_CONTEXT_BUDGET_TOKENS` 默认 2000 未变（`worker.py` 约 2767 行）。

**Rollout:** 无独立开关（等价于 stage A 的编译面修正；不改变 prompt 内容与预算）。
**Risks:** `render_legacy` 是双路径的兼容层，长期应收敛为单一渲染；任何
`legacy_style` 分支改动都要同时跑两组 context 测试。
