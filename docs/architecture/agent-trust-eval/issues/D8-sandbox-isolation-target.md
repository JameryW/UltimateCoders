# D8-sandbox-isolation-target

Status: **decided** (2026-10-24 — round-2 via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none)_
Blocks: P2 NetworkMode enforcement tickets; Landlock FS as a later ticket on the same map

## Question

隔离的实现靶点是什么：主机子进程上的 **Landlock + seccomp**、容器/CNI **egress 白名单**，还是先把已有 `NetworkMode` 接到真实 enforcement？「默认断网」与 LLM/API/NATS 出站如何并存，以免假安全感？

## Context

- `NetworkMode` 默认 Full，子进程未强制；T11 env allowlist 已覆盖密钥泄漏面的一部分。
- Worker 容器本身需要 NATS/Gateway；断网必须作用在**Agent 子进程**而非整个 worker。
- 用户 P2：Landlock + 默认断网。

## Resolution

**先让 NetworkMode 成真；Landlock FS 后置。**

1. P2 第一步：实现 `None` / `Restricted` / `Full` 的真实语义（可用 seccomp-bpf / netns 等），消灭「配置了 None 却不通」的假安全感。
2. 默认倾向 **离线**（`None` 或 `Restricted`，功能开关），LLM/API 出站走**显式 allowlist**；NATS/Gateway 属于 worker 常驻面，不与 Agent 子进程断网混为一谈。
3. 完整 **Landlock FS** 策略作为同地图的后续实现票，不阻塞网络 enforcement。
