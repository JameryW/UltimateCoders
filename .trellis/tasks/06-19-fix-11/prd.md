# PRD: 前后端交互11项逻辑与体验问题修复

## 背景

PR88 前后端交互代码深度分析发现11个问题（2个逻辑bug + 9个体验问题），需要跨前后端修复。

## 修复清单

### 🔴 逻辑问题

| # | 问题 | 修复方案 | 文件 |
|---|------|---------|------|
| 2 | SSE snapshot 的 events 字段直接替换，覆盖 gRPC 增量更新的 eventLog | 合并：保留增量事件在 snapshot events 前面，去重 | `useDashboard.ts` |
| 5 | gRPC listTasks 的 pending_task_count 硬编码为 0 | 从 gRPC response 读取，proto 已有该字段 | `useGrpcWeb.ts` |

### 🟡 体验问题

| # | 问题 | 修复方案 | 文件 |
|---|------|---------|------|
| 4 | allFailed 判断阈值 5 过于激进 | 要求核心端点(health+tasks)失败才判定全挂 | `App.tsx` |
| 6 | 双通道同逻辑事件 data 字段差异导致去重失败 | 统一 eventKey 逻辑，忽略 data 中不影响语义的字段差异 | `App.tsx` |
| 7 | SSE 重连 fetchInitial 覆盖 gRPC 增量 | fetchInitial 后用 mergeGrpcTasks 而非直接 setTasks | `App.tsx`, `useDashboard.ts` |
| 8 | Flush Pending 只走 REST 不走 gRPC | gRPC 连接时走 gRPC path | `App.tsx`, `useGrpcWeb.ts` |
| 9 | Pause/Resume gRPC 路径不立即更新前端状态 | 成功后立即更新 tasks state | `App.tsx` |
| 10 | 前端 eventLog 截断 200 与后端 500 不对齐 | 前端也改为 500 | `useDashboard.ts` |
| 1 | REST submit 乐观插入 subtaskCount=0 跳变 | REST 路径也传 subtask_count 和 subtasks | `TaskSubmitForm.tsx` |
| 3 | NATS submit 0.5s sleep 竞态 | 增加到 1.0s，并加重试 | `app.py` |
| 11 | SSE boolean vs gRPC 5状态指示不统一 | 统一为复合状态，ConnectionIndicator 显示更清晰 | `App.tsx`, `ConnectionIndicator.tsx` |

## 不改的部分

- 去重机制的 SSE id + content key 双轨设计本身是好的，只修 eventKey 的比较逻辑
- snapshot 增量合并的 field-level merge 逻辑保留
- NATS fallback 机制保留
