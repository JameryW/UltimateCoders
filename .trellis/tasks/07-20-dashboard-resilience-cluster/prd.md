# PRD: Dashboard 韧性簇（审计 #7/#9/#10）

## 背景

/loop 第 32 轮，dashboard 重审 MED/S 韧性簇。

## 清单（已核实）

### F73: connectionError 永不可达 + server down 报 "Invalid password"（审计 #7，MED/S）

`validateToken`（useAuth.ts:64-84）catch 所有错误返 false → 挂载 effect 的 catch（setConnectionError(true)）是死码，connectionError 恒 false → "Connection Error / Retry" 屏（App.tsx:347-358）不可达。backend down 时用户见登录表单，登录尝试报 **"Invalid password"**（网络故障被当密码错）。

修：validateToken 区分——Unauthenticated/PermissionDenied → `false`（认证拒绝）；其他错误 → **throw**（传输层失败透传）。挂载 effect catch 复活 → connectionError 屏可达。login()：try/catch validateToken，throw → `setConnectionError(true)` + return false（"Invalid password" 仅留真实认证拒绝）。

### F74: pause/resume/cancel/listTasks/search 无超时（审计 #9，MED/S）

仅 submitTask 有 30s AbortController（useGrpcWeb.ts:278）。server 接受 TCP 但 stall 时，pause/resume/cancel promise 永不 settle → App.tsx 乐观状态**永不回滚也无 toast**；listTasks 卡同步；SearchPanel `client.search` 永 "Searching…"。

修：useGrpcWeb 抽 `unaryWithTimeout(call, what)` helper（30s AbortController，AbortError → "<what> timed out after 30s"），应用到 listTasks/pauseTask/resumeTask/cancelTask；导出后 SearchPanel search 同用。

### F75: sync_required 每次页面加载最多触发一次（审计 #10，MED/S）

`needsSync` state 置 true 从不复位。第二次 sync_required → `setNeedsSync(true)` state no-op（同值）→ effect（deps `dashboard.needsSync`）不重跑 → 待处理的 needsSyncCountRef 增量直到其他 dep 碰巧变化才消费。

修：effect 处理后 `dashboard.setNeedsSync(false)` 复位 → 下次 true 是真状态变化 → effect 重跑（复位引发的额外一次运行 ref=0 早退，无害）。

## 验收

- 手动推理 + tsc（所触文件零新错，既有不变）。
- `npx tsc -p tsconfig.app.json --noEmit` + vite build（CI ci-dashboard 权威）。
- feature branch + PR + CI green。

## 不做（后续轮）

#8 FileBrowser 跨仓库导航竞态（MED/M，下轮）；#11-#15 LOW 杂项收尾轮。
