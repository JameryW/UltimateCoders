# T33 —— 钉住 WorkerService RPC → 花名册的字段映射（placement 的全部输入都从这条缝进来）

承接 **#683**。属**测试卫生线**（与 T21–T32 同类），**不依赖**外部「方案第 21 节」。

## 1. 起点与判据

placement 的输入**无一例外**来自 RPC 带进来的三个 proto 字段：

| placement 依赖 | proto 字段 | 映射发生在 |
|---|---|---|
| `recent_files`（亲和打分） | `WorkerHeartbeatRequest.recent_files` | `worker_heartbeat` → `heartbeat_with_signals` |
| `per_worker_topic`（可定向前提） | `WorkerHeartbeatRequest.per_worker_topic` | 同上 |
| `capabilities`（硬门） | `RegisterWorkerRequest.capabilities` | `register_worker` → `register_with_projects` |

`placement_target`（`crates/uc-grpc/src/worker_service.rs:440`）里三处都直接读花名册：
`dispatch_candidates(required, ...)`（能力门）→ `.filter(|w| w.per_worker_topic)` →
`PlacementCandidate { recent_files: w.recent_files.as_slice(), ... }`。

**但接缝无主**（2026-09-18 实测）：

- 7 个 placement 测试全部用**本地辅助**直接戳 registry，绕过 RPC：
  `registry_with`（`server.rs:8300-8313`）、`signalled`（`worker_service.rs:2019-2035`）。
- 3 个 `register_worker_rpc_*`（`worker_service.rs:1388` / `:1414` / `:1996`）走到 RPC，
  但只断言「接受/拒绝」，**从不要求花名册做决策**。

⇒ 与 T12、T32 **同形**：注入路径与生产路径分叉，套件全绿。

## 2. 消融证据（都在 `cargo test -j 1 -p uc-grpc --all-features` 下）

| 突变 | 重编译 | 结果 |
|---|---|---|
| `req.per_worker_topic` → `false` | ✅ | 249 passed / **0 failed（存活）** |
| `&req.recent_files` → `&[]` | ✅ | 249 passed / **0 failed（存活）** |
| `req.capabilities` → `Vec::new()` | ✅ | 249 passed / **0 failed（存活）** |

阳性对照：`PER_WORKER_SUBJECT_PREFIX`（`placement.rs:249` 的绝对钉）加 `zz` ⇒ **rc=101 / 6 failed**
⇒ 装置确有检出能力，上面的「绿」不是「没重编译」造成的。基线 rc=0（lib 249 + 集成 8）。
三次突变后按字节恢复，sha256 回 `4b6a4db8…`，工作树 0 行。

### 顺带发现（**不在本票范围**）

`placement.rs:303` 的 `assert_eq!(norm.len(), MAX_RECENT_FILES)` 是**符号自指**：常量
`64 → 640` 后仍全绿 ⇒ 它钉不住该常量的值。proto 未承诺这个数字（只说「gateway 重界」），
故记录不修；下一个人别把它当已覆盖。

## 3. 交付

在 `worker_service.rs` 的测试模块新增一条 `#[tokio::test]`：

1. `make_server()`（`worker_service.rs:1520`，`GrpcServer<LocalEngine>`）；
2. `register_worker(Request::new(RegisterWorkerRequest { capabilities: ["code"], contract_version: CONTRACT_VERSION, .. }))`；
3. `worker_heartbeat(Request::new(WorkerHeartbeatRequest { recent_files: ["src/auth.rs"], per_worker_topic: true, .. }))`；
4. `placement_target(&["code"], "", &["src/auth.rs"], &no_hosts(), &HashSet::new())`；
5. 断言 **字面量**：`worker_id == "w-rpc"`、`subject == "uc.subtask.execute.w.w-rpc"`、`affinity_hits == 1`。

**断言必须用字面量而非符号**（否则重犯上面那条自指失败）：`subject` 直接写全串。

## 4. 验收

| # | 判据 | 期望 |
|---|---|---|
| 1 | 新测试（未突变源码） | 绿 |
| 2 | 突变自检 m1 / m2 / m3 各自单独施加 | **三条各自让新测试变红**（修复前 0/3） |
| 3 | `cargo test -j 1 -p uc-grpc --all-features` | lib **249 → 250 passed**，0 failed |
| 4 | `check-tasks-refs` 语料钉值 | `(787, 788)` → **`(788, 789)`**（本票 `implement.jsonl` 的 prd 引用入语料；同 change 更新） |

## 5. 非目标

- **不改生产代码**：三处映射当前都是对的（逐行读过 `worker_heartbeat` / `register_worker`），本票只补钉。
- 不碰 `MAX_RECENT_FILES` 的符号自指。
- 不碰 P2 本体（仍阻塞于外部「方案第 21 节」原文）。
- 不加 live broker / live gateway e2e：本票的接缝在**进程内 RPC 调用**上，`make_server()` 已足够真实。
