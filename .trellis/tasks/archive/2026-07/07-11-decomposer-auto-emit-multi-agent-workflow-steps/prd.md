# Decomposer Auto-Emit Multi-Agent Workflow Steps

## Goal

让 OMP decomposer 能自动为 subtask 生成多 agent workflow steps（claude-code 写 → codex CR → claude-code revise 三步链），并把这些 steps 完整传递到 worker 执行。当前 `WorkflowStep` 在 Rust 类型/proto/conversions + Python worker 执行链已全部贯通（PR #213/#214），但 OMP TS 层是断点：decomposer 不产 steps，即使产了 `parseSubtaskOutput` 也丢弃。

这是 07-03 任务 PRD 中明确标注的 Out-of-Scope「后续 PR」项：`decomposer 自动产 steps：OMP decomposer.md 当前 schema 不含 steps，手动声明 steps 已可用。decomposer 自动生成是后续 PR。`

## What I already know (verified this session)

- `crates/uc-types/src/agent.rs:115` — `Subtask.steps: Vec<WorkflowStep>` 一等字段，空 = 向后兼容单步。
- `crates/uc-types/src/agent.rs:124-140` — `WorkflowStep { agent, prompt, agent_config_json?, abort_on_failure }`，prompt 模板支持 `{{prev_summary}}`/`{{prev_files}}`/`{{stepN.*}}`。
- `crates/uc-grpc/proto/engine.proto:381` — `repeated WorkflowStepProto steps = 14`。
- `crates/uc-grpc/src/conversions.rs:890,896-907,1269` — `step_to_proto`/`step_from_proto` 双向映射齐全。
- `python/ultimate_coders/agent/worker.py:984,1043` — `_execute_steps` 链式驱动已实现（PR #213），`{{prev_summary}}` 注入测试覆盖。
- `packages/uc-orchestrator/src/agents/decomposer.md` — output schema 仅 `id/description/depends_on/files`，**无 steps**。
- `packages/uc-orchestrator/src/orchestrator/scheduler.ts:13-33` — `SubtaskDef` interface **无 steps 字段**。
- `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:1201-1210` — `parseSubtaskOutput` 映射 decomposer JSON → SubtaskDef，**丢弃 steps**（即使 JSON 含 steps 也不读）。
- `packages/uc-orchestrator/src/orchestrator/task-bridge.ts:83,153` — 仅读 `st.steps?.length` 做 UI tag，但 SubtaskDef 无 steps → 恒为 0；**从不把 steps 送到 proto**。

结论：TS 层三处断点（decomposer schema / SubtaskDef / parseSubtaskOutput / task-bridge toProto），Rust+Python 全通。

## Assumptions (temporary)

- decomposer 用 LLM 判断哪些 subtask 值得 CR-revise 链（非所有 subtask 都要三步；简单单文件改动仍单步）。
- step 的 prompt 模板由 decomposer 生成，复用已实现的 `{{prev_summary}}`/`{{prev_files}}` 变量（worker 侧已支持，TS 无需新增模板引擎）。
- 不引入新依赖。

## Decisions (locked)

- **D1: TS SubtaskDef 加 `steps?: WorkflowStepDef[]`**。类型对齐 Rust `WorkflowStep`（agent/prompt/agent_config_json?/abort_on_failure）。可选字段，空 = 旧单步路径。
- **D2: decomposer.md output schema 加 `steps`**。每 subtask 可选 `steps` 数组，每项 `{agent, prompt, abort_on_failure?}`。Rules 区分何时产 steps（含 code-write 的 moderate/complex subtask 默认 claude-code→codex→claude-code；simple 单步）。
- **D3: parseSubtaskOutput 读 `st.steps`** 映射进 SubtaskDef.steps，未知字段忽略。
- **D4: task-bridge.ts toProto 把 SubtaskDef.steps 序列化进 proto `steps` field 14**。当前只读 length 做 tag，补写 proto 映射。

## Open Questions

(none — resolved below)

## Resolved

- **R1 (was D5)**: chain-detection rule lives in **decomposer prompt**, no code enforcement. decomposer misses → single step, no error. Code only maps whatever JSON arrives.
- **R2 (was D6)**: give decomposer a **fixed template-variable list** (`{{prev_summary}}`/`{{prev_files}}`/`{{stepN.summary}}`/`{{stepN.files}}`) + one example 3-step prompt. No free variable naming — worker only renders the known set.
- **R3 (was D7)**: `parseSubtaskOutput` shared by decomposerLocal + decomposerRemote — one edit covers both paths. No separate handling.

## Requirements (evolving)

- decomposer.md schema 含 steps；decomposer agent 知道何时产 steps 链。
- TS SubtaskDef + parseSubtaskOutput + task-bridge toProto 三处贯通 steps 到 proto。
- 端到端：手动构造含 steps 的 decomposer 输出 → orchestrator → gRPC → worker `_execute_steps` 跑通三步链。
- 向后兼容：无 steps 的 subtask 行为不变（现有 ts 测试 0 回归）。

## Acceptance Criteria

- [ ] decomposer.md output schema 含 `steps` 字段定义 + 何时产链的规则。
- [ ] `SubtaskDef` 加 `steps?`，TS 编译通过。
- [ ] `parseSubtaskOutput` 读 `st.steps` 进 SubtaskDef.steps。
- [ ] `task-bridge.ts` toProto 写 steps 进 proto field 14（UI tag 之外的真实映射）。
- [ ] 端到端测试：含 3-step 链的 decomposer JSON → worker 收到 3 steps 并按序执行（`{{prev_summary}}` 注入验证）。
- [ ] 无 steps 路径 0 回归（现有 scheduler/orchestrator/task-bridge ts 测试全绿）。

## Definition of Done

- [ ] decomposer.md schema + rules 更新
- [ ] scheduler.ts SubtaskDef + WorkflowStepDef 类型
- [ ] orchestrator.ts parseSubtaskOutput 读 steps
- [ ] task-bridge.ts toProto 写 steps
- [ ] ts 测试新增 + 现有 0 回归
- [ ] cargo check（Rust 侧无改动，仅确认 proto 已有 steps 不需动）

## Out of Scope

- step 间并行 / 条件跳转（沿用 07-03 决策：MVP 线性 only）
- 跨 worker step 编排
- step 级 agent_config 字段名 mismatch 统一（沿用 07-03 决策）
- decomposer 产 steps 的质量调优（首版给规则 + 示例，迭代后续）

## Technical Approach

数据流（补 TS 断点）:
1. `scheduler.ts`: `SubtaskDef` 加 `steps?: WorkflowStepDef[]`；新增 `interface WorkflowStepDef { agent; prompt; agent_config_json?; abort_on_failure? }`（对齐 Rust）。
2. `decomposer.md`: output schema `subtasks[].items` 加 `steps` 可选；Rules 区分 simple（无 steps）/ moderate+（默认 claude-code→codex→claude-code 三步，codex step prompt 注入 `{{prev_summary}}`/`{{prev_files}}`）。
3. `orchestrator.ts:parseSubtaskOutput`: `st.steps` 映射进 SubtaskDef.steps（未知字段忽略，向后兼容）。
4. `task-bridge.ts`: toProto 时把 `def.steps` 序列化进 proto `steps`（field 14），不只读 length。
5. 测试：构造 decomposer JSON 含 3-step 链 → 断言 SubtaskDef.steps.length===3 → 断言 proto payload 含 steps → （可选）mock worker `_execute_steps` 断言按序调用 + `{{prev_summary}}` 注入。

## Technical Notes

- Rust proto `steps = 14` + conversions 已通（PR #213），TS 这边补齐即端到端通。
- `{{prev_summary}}` 等模板变量 worker 侧已实现渲染（worker.py `_render_step_prompt`），TS 只需透传 prompt 字符串，不渲染。
- decomposerRemote 与 decomposerLocal 共用 parseSubtaskOutput，改一处覆盖两条路径。
