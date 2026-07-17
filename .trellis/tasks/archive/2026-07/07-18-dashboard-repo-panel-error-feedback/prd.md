# PRD: Dashboard Repo Panel Error Feedback

## 背景

dashboard agent 审计 finding #16。`RepoManagementPanel.tsx` handleReindex/handleRemove catch 块空，失败静默。

## Bug

- handleReindex (L46-65)：`indexRepo` 失败 → `catch {}` 空 → loading 停，无任何反馈。用户不知 reindex 失败。
- handleRemove (L67-81)：`removeIndex` 失败 → 同。

indexRepo/removeIndex（endpoints.ts L45/69）无内部 toast。失败真静默。

## 改

catch 块加 `showToast(..., "error")`（全局 toast，App.tsx 等已用）。reindex 失败：`Reindex failed: ${err}`；remove 失败：`Remove failed: ${err}`。可选成功 toast（reindex/remove 成功后）——PRD 决定加成功 toast 提升反馈。

## 验收

- vite build + `tsc -p tsconfig.app.json` RepoManagementPanel clean。
- feature branch + PR。
- PR 后查 CI。

## 不做

- 不改 indexRepo/removeIndex 实现。
- 不碰 Rust。
