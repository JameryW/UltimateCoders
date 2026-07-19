# PRD: Overlay Clipboard Yank

## 背景

/loop 第 10 轮。审计候选 clipboard（S，第 7 轮可行性确认）。用户经常把 task/subtask id、error 文本粘到别处（聊天、工单、终端），overlay 里只能看着手打。

vendor `copyToClipboard`（coding-agent/utils/clipboard.ts）存在但包 index 未导出；vendor 是 submodule（改 = submodule 提交 + 指针 bump，重）。**选自包含方案**：uc-orchestrator 内 ~20 行 `ui/clipboard.ts`。

## 改

### F21: copyText 工具（ui/clipboard.ts 新文件）

```
darwin  → pbcopy execFileSync（exit code 可验证；Terminal.app 无 OSC52 支持，pbcopy 是唯一可靠路径）
其他    → OSC52 转义 \x1b]52;c;<base64>\x07 写 stdout（iTerm2/WezTerm/xterm/mlterm 等；fire-and-forget，TTY 时返回 true）
```

vendor 验证过 OSC52 会话中直写 stdout 安全（绕 pi-tui renderer，自用控制器同款）。ponytail 上限：非 darwin 无 native fallback（xclip/wl-copy 环境差异大，OSC52 覆盖面够；不行再加）。

### F22: overlay `y`/`Y` 键

- **task-list-overlay**: list mode `y` 复制 cursor task id；detail mode `y` 复制 detail task id。flashMsg `copied <id8>` / `copy failed`。
- **subtask-tree-overlay**: `y` 复制 cursor subtask id；`Y` 复制 cursor subtask error 文本（无 error → flash "no error to copy"）。`r/R` 已是 retry，不冲突。
- hint 行加 `y copy`（full hint，compact <60 版不加——S5 窄屏只留核心键）。
- 可注入 `copy?: (text: string) => boolean` option（默认 copyText）——selfcheck 注入 fake 不污染真剪贴板。

## 验收

- 两 selfcheck：fake copier 注入 → `y` flash "copied …" 且 copier 收到正确 id；`Y` 无 error → "no error to copy"；copy 返回 false → "copy failed" flash。
- 既有 selfcheck 全绿（y 原为死键，无冲突断言）。
- bun test test src + tsc（src/ 零错误）。
- 本地 commit（网络恢复后 push + PR + CI——GitHub TLS 故障中，round 9 归档 commit 亦待推）。

## 不做

- vendor index 导出方案（submodule 改动重）。
- 非 darwin native fallback（xclip/wl-copy）。
- /uc search path 复制（审计提及，search 输出走 notify toast 非 overlay，接线不同，下轮评估）。
