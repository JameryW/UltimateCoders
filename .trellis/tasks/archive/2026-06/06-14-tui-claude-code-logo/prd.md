# TUI界面重构：Claude Code风格布局与Logo设计

## Goal

重构 UltimateCoders TUI 界面，参考 Claude Code CLI 的设计风格，打造更专业、更沉浸的终端交互体验，并设计专属 Logo 作为品牌标识。

## Requirements

* 设计 UltimateCoders 专属 Logo：组合式（2-3行紧凑 ASCII art "UC" + 名称 + 版本号）
* 重构 Header 区域：展示 Logo + 版本号，极简风格
* 改为对话式交互：用户输入和系统输出交替出现在主区域，底部 `>` 提示符输入
* 布局：主区域对话流（左，宽）+ 右侧 SubtaskTree 面板（窄）
* 参照 Claude Code 风格优化整体视觉（深色主题、极简边框、专业配色）
* 保持现有功能不变（SubtaskTree 状态更新、事件监听、任务提交/执行）

## Acceptance Criteria

* [ ] Logo 在终端中清晰可辨，组合式 ASCII "UC" + "UltimateCoders" + 版本号
* [ ] Header 区域展示 Logo + 版本号，2-3行高度
* [ ] 主区域为对话式交互：用户输入以 `>` 前缀显示，系统输出紧跟其后
* [ ] 右侧面板保留 SubtaskTree，实时状态更新正常
* [ ] 底部输入区为 `>` 提示符的单行输入
* [ ] StatusBar 保留在底部，显示 Worker/Backend/Progress
* [ ] 深色主题，极简边框风格
* [ ] 所有现有功能正常工作（任务提交、子任务执行、事件监听）
* [ ] 现有测试通过

## Definition of Done

* Lint / typecheck / CI green
* 现有测试通过
* Logo 渲染效果验证

## Technical Approach

### 布局结构
```
┌─ Header (Logo + Version) ─────────────────────────────┐
│  ╔═╗╦ ╦╔═╗╔═╗                                        │
│  ║  ╚╦╝║╣ ╚═╗   UltimateCoders v0.1.0               │
│  ╚═╝ ╩ ╚═╝╚═╝                                       │
├──────────────────────────────┬────────────────────────┤
│ ChatLog (对话流)              │ SubtaskTree            │
│ > Fix the bug in main.rs     │ ⚡ Subtasks [2/5 40%]  │
│ [14:30:02] Decomposing...    │ ✅ 1. Analyze bug      │
│ [14:30:05] Subtask started   │ 🔄 2. Write fix        │
│ [14:30:08] Tool call: search │ ⏳ 3. Run tests        │
│ ...                          │ ⏳ 4. Review            │
│                              │ ⏳ 5. Commit            │
├──────────────────────────────┴────────────────────────┤
│ > [TaskInput]                                         │
├───────────────────────────────────────────────────────┤
│ Worker: local-sandbox │ Backend: subprocess │ 2/5     │
└───────────────────────────────────────────────────────┘
```

### 关键改动

1. **新增 LogoHeader widget** — 渲染 ASCII art Logo + 版本号，dock: top
2. **重构 OutputLog → ChatLog** — 对话式输出，用户输入以 `>` 前缀回显，系统输出紧跟
3. **重构 TaskInput** — 添加 `>` 前缀提示符，深色背景
4. **布局调整** — Horizontal(ChatLog, SubtaskTree)，ChatLog 占 2fr，SubtaskTree 占 1fr
5. **主题优化** — 深色配色、极简边框、Claude Code 风格间距

### Logo 设计
- ASCII art "UC" 缩写（2-3行紧凑版）
- 右侧 "UltimateCoders" + 版本号
- 使用 Rich 库渲染，支持颜色

## Decision (ADR-lite)

**Context**: 需要确定 TUI 的视觉风格和交互模式
**Decision**: 
- Logo: 组合式（小 ASCII "UC" + 文字名称 + 版本号）
- Header: Logo + 版本号，极简
- 交互: 对话式（用户输入 + 系统输出交替）
- 布局: 主区域对话 + 右侧 SubtaskTree 面板
**Consequences**: 
- ChatLog 替代 OutputLog，交互逻辑需重构
- 用户输入回显到 ChatLog，Input widget 仅做输入捕获
- 右侧面板宽度固定比例，小终端可能需要最小宽度保护

## Out of Scope

* 功能性变更（不增加新功能）
* Light 主题支持
* Web Dashboard 改动
* SubtaskTree 可折叠/可切换

## Technical Notes

* 关键文件：`python/ultimate_coders/tui/app.py`, `python/ultimate_coders/tui/widgets.py`
* Textual CSS 用于样式控制
* Rich 库用于 Logo 渲染和颜色
* 版本号从 `ultimate_coders.__version__` 读取
* Claude Code 使用 Ink（React CLI），我们用 Textual，风格借鉴但实现不同
