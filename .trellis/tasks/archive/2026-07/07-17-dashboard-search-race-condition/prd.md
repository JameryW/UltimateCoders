# PRD: Dashboard Search Race Condition

## 背景

dashboard agent 审计 finding #8。`SearchPanel.handleSearch`（`dashboard/src/components/panels/SearchPanel.tsx:43-66`）是 async gRPC-Web unary 调用，无 request-id guard 或 AbortController。

## Bug

用户快速触发两次 search：
1. query A → Enter → `client.search(A)` in-flight（慢）
2. query B → Enter → `client.search(B)` in-flight（快）
3. B 先 resolve → `setResults(B)`
4. A 后 resolve → `setResults(A)` 覆盖

结果：UI 显示旧 query A 的结果，但输入框是 query B。用户困惑。

同问题影响 `setSearched`/`setError` 状态。

## 改

request-id guard（stdlib 模式，最小）：
- 模块级或 ref 持 `latestReqId`，每次 handleSearch 自增，resolve 后比对——非 latest 则丢弃 setResults。
- 不用 AbortController：Connect gRPC-Web client 的 signal 透传未验证，reqId guard 更稳且 transport 无关。

## 验收

- selfcheck（若可抽）或手动推理：两次并发 search，慢的后 resolve 不覆盖快的结果。
- vite build + `tsc -p tsconfig.app.json` 改动文件 clean（CI tsc 是 no-op，见 [[dashboard-ci-tsc-noop]]）。
- feature branch + PR（[[git-workflow-pr-only]]）。
- PR 后查 CI（[[pr-ci-check-workflow]]）。

## 不做

- 不改 transport/client 创建逻辑。
- 不碰 auth（#4，另立）。
- 不改 Rust 层。
