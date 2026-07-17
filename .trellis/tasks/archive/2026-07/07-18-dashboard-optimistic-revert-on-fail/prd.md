# PRD: Dashboard Optimistic Revert on gRPC Fail

## 背景

dashboard agent 审计 finding #17。`App.tsx` handlePauseTask/Resume/Cancel (L272-310) 乐观更新后调 gRPC，失败回滚不全。

## Bug

三 handler 模式：
```
optimisticStatusUpdate(taskId, NEW)
try {
  if (grpcState === "connected") {
    const r = await grpcXxx(taskId);
    if (r.success) showToast ok;
    else showToast err;   // ← 未回滚乐观状态
  }
} catch (e) {
  optimisticStatusUpdate(taskId, OLD);  // ← 仅异常回滚
}
```

**真缺口**：`r.success === false`（gRPC 调用成功但服务端拒绝，如 task 状态不对）只 showToast error，**不回滚乐观状态**。任务前端显示 paused/cancelled 但服务端未改。sync 前用户看错状态、可能误操作。

catch 路径已回滚。grpcState 非 connected 路径乐观是临时（sync 纠正），不修。

## 改

`r.success === false` 时回滚乐观状态到原值（pause→in_progress, resume→paused, cancel→in_progress）。三 handler 对称补。

## 验收

- 逻辑推理：r.success false → 乐观回滚 + toast error。
- vite build + `tsc -p tsconfig.app.json` App.tsx 改动 clean（CI tsc no-op，见 [[dashboard-ci-tsc-noop]]）。
- feature branch + PR（[[git-workflow-pr-only]]）。
- PR 后查 CI（[[pr-ci-check-workflow]]）。

## 不做

- 不动 grpcState 非 connected 路径（sync 兜底）。
- 不改 optimisticStatusUpdate 实现。
- 不碰 Rust。
