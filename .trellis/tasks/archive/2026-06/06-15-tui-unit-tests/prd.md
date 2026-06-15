# TUI Unit Tests

## Goal

为 TUI 重构的核心模块补充单元测试，覆盖 reducer 状态转换、事件格式化、CJK 截断、离线 timer 清理、符号策略等关键逻辑。

## Requirements

1. **reducer.ts 测试** — 覆盖所有 action type 的状态转换
   - ADD_MESSAGES (含 2000 上限)
   - SET_SUBTASKS, UPDATE_SUBTASK_STATUS
   - SCROLL_UP/SCROLL_DOWN (scrollTick 递增、followLog 变化)
   - ADD_INPUT_HISTORY (去重、50 条上限)
   - SET_EVENT_FILTER
   - CLEAR_TASK, CLEAR_LOG
   - ADD_OFFLINE_TIMER, CLEAR_OFFLINE_TIMERS
   - 边界情况：空 state、重复 action、负 offset

2. **formatters.ts 测试** — 覆盖 formatTaskEvent/formatTaskEvents
   - 每个 event.type 的输出格式
   - eventType 字段正确传递
   - null 返回（空事件）
   - 中文字符串不被截断

3. **symbols.ts 测试** — 覆盖 getSymbols/resolveSymbolMode
   - unicode 模式返回完整符号
   - ascii 模式返回安全符号
   - auto 模式：CI=true → ascii, TERM=xterm-256color → unicode

4. **SubtaskTree truncateToWidth 测试** — 覆盖 CJK 截断
   - 短文本不截断
   - 长英文截断加 …
   - CJK 字符正确按 grapheme 截断
   - 组合字符/ZWJ emoji 不被拆分

5. **ChatLog 过滤逻辑测试** — 覆盖 eventFilter 行为
   - all: 显示全部
   - task: 只显示 task_ 前缀事件
   - subtask: 只显示 subtask_ 前缀事件
   - error: 只显示 _failed 事件
   - 用户消息始终显示

## Acceptance Criteria

- [x] 所有测试通过 (57/57)
- [x] 覆盖 reducer 所有 action type (20 tests)
- [x] 覆盖 formatters 所有 event.type (13 tests)
- [x] 覆盖 symbols 三种模式 (9 tests)
- [x] 覆盖 CJK 截断边界情况 (8 tests)
- [x] 覆盖事件过滤逻辑 (7 tests)
- [x] npm run typecheck 保持通过

## Out of Scope

- 不测试 React 组件渲染（Ink 组件难以在 Jest 中测试）
- 不测试 gRPC 客户端（需要 mock proto）
- 不测试 useInput 交互（集成测试范畴）

## Technical Notes

- 测试框架：vitest（TUI 项目已有 vitest 配置或需要添加）
- 纯函数测试：reducer、formatters、symbols、truncateToWidth、eventFilter
- 不需要 React 渲染环境
