# D2 — 网关组合块也走同一个 Context Compiler，并留下 manifest

Status: decided
Decided: 2026-11-15

## Question

过去 worker 的上下文来自两条路：本地依赖注入器（`ContextInjector`）与 Gateway 组合
的「completed subtasks」块。后者**不本地编译、不写 context manifest**——
`docs/agent-os-architecture.md` §6 曾明文写着「gateway 组合块路径不本地编译，因而不留
记录——空历史而非伪造记录」。统一 Context Compiler 之后，这条旧规则还成立吗？

## Decision

**不成立，本决策取代它。worker 的两条上下文来源都走同一个 `compile_context`，且都
写同一份 context manifest。**

1. **单一编译面**：`from_dependency_results()` 与 `from_gateway_context_block()`
   只是适配器，把输入变成 `ContextItem`；预算分配、内容去重、风险预留、tier 打包
   只在 `compile_context` 里做一次（`python/ultimate_coders/agent/context_compiler.py`）。
2. **manifest 无缺口**：两条路径之外，`from_search_block()` / `from_hy_memory_block()`
   同样并入同一次编译；`self._last_context_manifest = manifest.summary()` 与
   `await self._record_context_manifest(...)` 在两条路径上都执行。理由写在代码注释里：
   「a compiled prompt with no manifest is an audit hole」。
3. **渲染兼容**：`ContextManifest.render_legacy(legacy_style=..., gateway_truncated=...)`
   保留两个历史头部（worker 注入器 / gateway 组合块）与两条截断提示语；旧 prompt
   逐字节不变，只有当预算真的丢了内容才追加标记
   （`tests/python/test_context_compiler.py`）。
4. **预算常量**：`UC_CONTEXT_BUDGET_TOKENS`，默认 2000
   （`worker.py::_context_budget_tokens`）；审计落库 key 仍是 `task:subtask:attempt`
   （`ContextManifestOutbox`，`python/ultimate_coders/context_outbox.py`）。

## Why

- **审计不能有空历史缺口**：「gateway 路径不记录」是兼容时期的妥协，adapter 化之后
  它的成本为零（一次 `compile_context` 调用 + 一次 outbox 写入）。
- **同一输入同一判定**：两条路径如果各自做预算/去重，排序与丢弃原因就会分叉，
  manifest 之间不可比；编译器已经是确定性的唯一实现。
- **prompt 逐字兼容**：`render_legacy` 保证升级不改变模型实际看到的内容，使 rollout
  可以在 stage B（只采集、不改 prompt）先观察。

## Boundary（本决策不做什么）

- 不改变 Gateway 侧组合逻辑本身（依赖摘要仍由 Gateway 写，worker 只消费）。
- 不新增 prompt 内容、不改 manifest schema、不改 dashboard 端点契约。
- 不保证两条路径的 token 数相同（输入本就不同）；只保证同一输入产生同一结果。

## Implementation ticket

T03（统一编译面 + 双路径 manifest）。相关文档更正：
`docs/agent-os-architecture.md` §6 的「gateway 组合块路径不本地编译」一句已被本决策
取代，改为指向本节。
