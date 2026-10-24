# D8-sandbox-isolation-target

Status: open (decision ticket — not implementation)
Type: grilling
Blocked by: _(none — frontier; P2)_
Blocks: P2 Landlock/断网 implementation tickets; must decide before wiring dead `NetworkMode`

## Question

隔离的实现靶点是什么：主机子进程上的 **Landlock + seccomp**、容器/CNI **egress 白名单**，还是先把已有 `NetworkMode` 接到真实 enforcement？「默认断网」与 LLM/API/NATS 出站如何并存，以免假安全感？

## Context

- `NetworkMode` 默认 Full，子进程未强制；T11 env allowlist 已覆盖密钥泄漏面的一部分。
- Worker 容器本身需要 NATS/Gateway；断网必须作用在**Agent 子进程**而非整个 worker。
- 用户 P2：Landlock + 默认断网。

## Options

1. **主机 Landlock/seccomp + 默认无网**：Agent 子进程默认 `NetworkMode::None`；显式 allowlist 域名/端口给 LLM API。
2. **容器侧**：每任务短时 sidecar/network namespace；worker 常驻网保留。
3. **先 enforcement 枚举**：只实现 `None`/`Restricted`/`Full` 的真实语义（可用 seccomp-bpf / netns），Landlock FS 第二阶段。

## Proposed (from intake — needs human confirm)

➡️ **选项 3 → 1**：P2 先消灭「配置了 None 却不通」的假安全感；默认改为 Restricted/None（功能开关），LLM 出站走明确 allowlist。完整 Landlock FS 策略可作为同地图的后续实现票，不阻塞网络 enforcement。

## Resolution

(fill in when decided)
