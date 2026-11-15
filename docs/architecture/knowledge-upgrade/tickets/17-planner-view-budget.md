# 17 — planner 视图预算开关 (T17)

**What to build:** 给 gateway 组装规划视图（`ContextBlock`）的那一侧一个**可调但
有硬上限**的字节预算：D6 的 1500–1800 tokens 是实验起点，不能因为「想跑 A-B」就把
它写死进代码，也不能因为调大 env 就让 dispatch 载荷无界增长。开关是 env
`UC_PLANNER_VIEW_BUDGET_BYTES`，默认等于既有 8 KiB 硬上限，低于下限抬回下限。

**Blocked by:** T03（统一编译面）、D6（token 预算决策）
**Spec:** [D6-token-budgets.md](../issues/D6-token-budgets.md)、[spec-p0.md](../spec-p0.md)
**Status:** implemented（`crates/uc-types/src/envelope.rs`：`planner_view_budget_bytes_from`、
`planner_view_budget_bytes`、`ContextBlock::compose_with_budget`；`compose` 走 env 默认。
本机 crates.io 不可达、无本地 registry 缓存，`cargo test` 未能执行；已用独立 `rustc`
提取同逻辑验证解析/clamp/小预算打包；CI 需跑 `cargo test -p uc-types envelope`）

**Implementation**

1. 常量：`CONTEXT_BLOCK_MAX_BYTES = 8 * 1024`（仍是硬上限）、
   `CONTEXT_BLOCK_MIN_BYTES = 512`（低于此值的 env 抬回下限）、
   `PLANNER_VIEW_BUDGET_ENV = "UC_PLANNER_VIEW_BUDGET_BYTES"`。
2. `planner_view_budget_bytes_from(Option<&str>) -> usize`：trim → parse；空/非法/
   缺失都回落到 8192（失败方向是「保持现状」，不是「关掉上下文」）；结果
   clamp 到 512..=8192。
3. `planner_view_budget_bytes()` 只读 env；`ContextBlock::compose()` 调
   `compose_with_budget(entries, planner_view_budget_bytes())`，原有行为（默认
   8192）逐字节不变；`compose_with_budget` 自己再 clamp 一次，所以任何调用方都
   不能突破硬上限。
4. Rust 单测（`envelope.rs` tests）：解析/空白/非法值、区间值（6144）生效、
   超大值被降到上限、`0` 被抬到下限、env 读链、显式小预算只装得下部分 entry 且
   `truncated`、超大/极小预算被 clamp。既有「8 KiB 内、整 entry 不拆分」用例不变。

**Acceptance gates**

- [x] 未设 env 时 `compose` 与升级前行为一致（预算 8192）。
- [x] 合法 mid-range env 值改变打包上限（6144 生效）。
- [x] 非法/空/超出区间的 env 值 fail-safe：回落默认或 clamp，不会得到 0/无界。
- [x] 打包永不拆分单个 entry；溢出只丢整 entry 并置 `truncated`。
- [ ] CI 执行 `cargo test -p uc-types envelope`（本机无网络，未验）。

**Rollout:** 默认值即现状，无需放量门；A-B 时用 env 调 6144–7200（约 1500–1800
tokens 的 4 chars/token 估算）即可，两边用同一 `UC_PLANNER_VIEW_BUDGET_BYTES` 之外
只改知识层 stage。
**Risks:** 4 chars/token 是启发式，真实 token 数随模型 tokenizer 变化，预算单位是
字节不是 token；gateway 的 `ContextEntry.summary` 仍由上游截断，本开关只控制视图
总量。env 值只影响 planner view，不改 worker 的 `UC_CONTEXT_BUDGET_TOKENS`。
