# PRD: 合入2个未合并分支到main

## 背景
主分支有2个远程分支包含未合并的改动，需要合入。

## 待合并分支

### 1. fix/dashboard-interaction-round3
- **内容**: 6 files, +41/-6
- **改动**: Dashboard 交互修复第三轮
  - gRPC 数值/布尔类型转换 (useGrpcWeb.ts)
  - A11Y 改进 (InteractionLog, TaskDetail)
  - exhausted 状态处理 (TasksPanel)
  - ConnectionIndicator 增强
  - useDashboard hook 改进

### 2. feat/engineapi-task-methods
- **内容**: 9 files, +321/-54
- **改动**: TUI 增强
  - Ctrl+W 焦点切换
  - Alt+Enter 换行
  - undo/redo 支持
  - word 导航 (Ctrl+Left/Right)
  - Home/End 光标跳转
  - paste 支持
  - CJK 输入改进
  - keymap 测试更新

## 执行计划
1. 从 main 创建合并分支
2. cherry-pick 或 merge fix/dashboard-interaction-round3
3. cherry-pick 或 merge feat/engineapi-task-methods
4. 解决冲突（如有）
5. cargo check + 前端构建验证
6. 提交 PR
