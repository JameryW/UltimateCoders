# Agent Trust / Eval 缺口评估件

> 状态：**勘察完成；决策 D1–D8 已拍板（2026-10-24）**（P0 round-1；P1/P2 round-2 via「继续」）。本文件不是实现计划，也不是工单，只回答三个问题：用户列出的缺口与代码现状是否一致、P0–P2 路线上有哪些必须显式拍板的取舍、起步形态建议是什么。
>
> 输入：用户提供的缺口清单与优先级（评测 / 评审 / 防作弊 / 隔离 / 记忆 / 选择偏差）。
> 现状勘察基于 `main` @ `0d580422`。所有「现状」结论均附文件路径，可复核。
> 起步：本评估件 + `docs/architecture/agent-trust-eval/` 版本化 wayfinder 地图与决策票（`.scratch/` 在本仓库被 gitignore，故地图落在 docs 下）。**决策票不是实现工作，不得开 Trellis 任务。** GitHub 地图发布需另一次显式授权（仓库尚无 `wayfinder:*` label）。

## 一、一句话结论

用户列出的主要缺口在仓内**大体成立**。P0 不是「再加一个评测脚本」，而是先把**搜索 / 挑选 / 报告**三套基准与**正确性信任边界**定死，再谈近似评测和评审器。现有资产（指纹保护的 `BenchmarkRunner`、`Oracle`、env allowlist、Hy-Memory SDK 封装、`NetworkMode` 枚举、F8「部分」记录）足以承接，但多数开关未接到主路径或未强制执行。

## 二、缺口核实（相对用户原文）

| # | 用户主张 | 核实 | 证据要点 |
|---|----------|------|----------|
| G1 | 没有 DuoDiscover 式秒级近似评测 + 排序一致性检验 | **确认** | 全仓无 DuoDiscover / rank-agreement / Spearman 等；唯一验收路径是完整 `BenchmarkRunner`（warmup + ≥3 reps）+ `Oracle`（`inference/benchmark.py`、`oracle.py`；`docs/inference-infra.md`） |
| G2 | `review.py` 仅解析格式；无按失败类型的阈值 / 交叉验证 | **确认** | `python/ultimate_coders/agent/review.py` 正好 35 行，只校验 `approved`/`issues`/`suggestions`；失败分类在 `routing.py`，与评审阈值正交 |
| G3 | 「验证器错误放行」仍部分依赖自报正确性；文档标部分覆盖 | **部分确认** | `docs/agent-os-architecture.md` F8 状态为「部分」，剩余缺口写的是 **harness 自报 `correctness` attestation**（不是字面「部分覆盖」）。Oracle 以 `BenchmarkResult.correctness` 为硬门；harness 可由 Agent 生成（`metainfer_service/coding.py`） |
| G4 | 无 Landlock / seccomp；无默认断网 | **确认** | `SubprocessSandbox` 文档写明无 FS/网络隔离；`NetworkMode` 默认 `Full` 且 Python/Rust 子进程路径未强制执行；T11 env allowlist 已落地，但 PRD 明确把 seccomp/网络隔离列为非目标 |
| G5 | `hy_memory.py` 写好了但 `worker.py` 未调用 | **确认** | `worker.py` 零引用；上下文走 engine memory / search / context compiler；Hy-Memory 经 `engine_mcp` 工具与 `CodingMemory` API 可选接入（`docs/hy-memory.md`） |
| G6 | Oracle 可能用同一套基准既挑候选又报告提升（选择偏差） | **确认（结构属性）** | MetaInfer 取上游 ranked `library[0]` 再交 UC Oracle；`OptimizationWorkflow` 用同一 `BenchmarkRunner` 做迭代反馈与最终 uplift（`workflow.py`、`metainfer_service/execution.py`）；无 hold-out / search-vs-report 分裂 |

相关但用户未单独列出、决策时必须对齐的现状：

- `contract.py` 只做**契约文本摘要版本**（F6），**没有**结构化完成条件；完成相关能力散落在 `review.py`、`step_condition`、`verify_command`。
- Progress Detector 明确「本仓库没有 replan 原语」（`docs/agent-os-architecture.md`、`progress.py`）；停滞 → `loop_detected`，不换 Agent 重规划。

## 三、已有可复用资产（不要重写）

1. **保护基准 + Oracle**：指纹、`protected_paths`、噪声/零值拒绝、dispersion 门槛（F8 已覆盖一半）。
2. **Sandbox env allowlist（T11）**：子进程环境 deny-by-default；与 Landlock 正交。
3. **`NetworkMode` 枚举**：配置面已在，缺 enforcement（避免留下假安全感）。
4. **Hy-Memory 封装**：`CodingMemory`、doctor/preflight、compose override、MCP tools、集成测试。
5. **分层 Memory + Context Compiler**：Worker 主路径已有；Hy-Memory 是认知抽取层，不是替换。
6. **Failure routing**：`routing.py` 已有失败类型 → 模型路由，可与「按失败类型评审器」对齐，但不是同一层。

## 四、必须显式拍板的取舍（决策票）

**D1 · 三套基准的语义与可见性（P0）— ✅ 已决**  
P0 两分（select vs hold-out report）；search 可暂与 select 共用但标 `eval_role`；对外 uplift 只用 report；P1 再严格三分。详见地图票 D1。

**D2 · 独立正确性信任边界（P0，关闭 F8 剩余）— ✅ 已决**  
双闸：指纹 attestation + Agent 不可写环境独立重跑；Agent 生成 harness 不得单独关正确性门。详见地图票 D2。

**D3 · 完成条件的家（P0）— ✅ 已决**  
digest 与 `completion_conditions` 分命名空间、从 contract 包导出；非 LLM 判定；与 verify/step_condition/review 收敛为条件类型，不另立权威。详见地图票 D3。

**D4 · 近似评测启用门（P1）— ✅ 已决**  
剪枝-only；Spearman/Kendall 在 report hold-out 过门后才开开关；uplift 禁引近似分；bench/code 变更复测失败则自动关。详见地图票 D4。

**D5 · 失败类型评审器与阈值（P1）— ✅ 已决**  
taxonomy 对齐 `routing.py`；一类一 reviewer + CV 阈值；无标注则该类型关闭；禁止默认 0.5「已校准」。详见地图票 D5。

**D6 · Hy-Memory 接入形态与合规（P1）— ✅ 已决**  
默认 MCP-only；`UC_HY_MEMORY_AUTO_CONTEXT=1` 才自动注入；supersede-not-delete 硬约束；钉版本、合规前关。详见地图票 D6。

**D7 · 停滞重派 / replan 原语归属（P2）— ✅ 已决**  
MVP：同契约换 Agent 重派一次后人工；真 replan（新分解）等 D3 完成条件落地。详见地图票 D7。

**D8 · 沙箱隔离目标（P2）— ✅ 已决**  
先让 `NetworkMode` None/Restricted/Full 成真（默认偏离线 + LLM allowlist）；Landlock FS 后置同地图票。详见地图票 D8。

## 五、建议的起步与风险

**起步**：决策 D1–D8 已闭环；经 `$to-spec` / `$to-tickets` 开垂直切片实现票（P0 先于 P1/P2）。ontology MCP / README 数字 CI 仍在雾区，待单独毕业。

**站立偏好（写入地图 Notes）**：

- 新模块默认**功能开关关闭**。
- 近似评测与真实评测的相关性必须**可定期复测**；代码一改就可能漂。
- 每种失败类型评审器需要**人工标注**才能选阈值——没有标注就不要假装有 CV 阈值。
- Hy-Memory 是外部 SDK：钉版本、密钥不进 argv、合规审查外发。

**明确不在本评估件内交付**：实现代码、Trellis 任务、GitHub 地图 issue（待授权）。

## 六、与用户优先级的映射

| 优先级 | 用户项 | 决策依赖 |
|--------|--------|----------|
| P0 | 三套 benchmark 分开 | D1 |
| P0 | 评测在 Agent 改不到的环境重跑 | D2 |
| P0 | `contract.py` 完成条件等结构化字段 | D3 |
| P1 | 快速近似评测 + 统计检验 | D4 ← D1 |
| P1 | 按失败类型评审器 + CV 阈值 | D5 |
| P1 | Hy-Memory 接入 + 取代标记 | D6 |
| P2 | 停滞换 Agent 重派 | D7 |
| P2 | Landlock + 默认断网 | D8 |
| P2 | 项目本体 MCP / README 数字 CI | 雾区（待 P0 后毕业） |
