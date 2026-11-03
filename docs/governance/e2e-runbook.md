# 治理面端到端验收手册（需要 Docker / NATS）

> **状态说明（诚实）**：这份手册写于一台**没有 Docker、没有 NATS、没有 psql** 的机器上。
> 下面每一条命令都给出了**预期输出**，但作者本人**没有在本机执行过**它们——能执行的是单元/集成测试
> （`cargo test`、`pytest tests/python`，CI 全绿）。请把它当作「可执行步骤 + 判定标准」，而不是「已验证记录」。
> 每条都标注了**失败时最可能的原因**，因为 E2E 的失败很少是「代码错了」。

---

## 0. 前置条件

| 需要 | 检查命令 | 期望 |
| --- | --- | --- |
| Docker + Compose | `docker compose version` | v2.x |
| NATS 客户端（可选，用于手工 `request`） | `nats --version` | 任意版本；没有也能用 Python 客户端 |
| Python 环境（跑验收脚本） | `.venv/Scripts/python.exe --version`（Windows）/ `python3 --version` | 3.9+ |
| 仓库根目录 | `pwd` | 含 `crates/`、`python/`、`docker/` |

所有命令都在**仓库根目录**执行。

---

## 1. 起一套带治理面的集群

### 1.1 路径 A：Compose（推荐）

```bash
# 1) 本地存储 + NATS + 网关（容器形态；内存后备也可用）
./run-gateway.sh up --docker

# 2) 至少一个 worker（同宿主；跨宿主见 AGENTS.md 的 UC_SCALE_HOSTS）
docker compose -f docker/docker-compose.yml --profile worker up -d --scale worker=2 worker

# 3) 看网关日志确认治理面已起
./run-gateway.sh logs | grep -E "Governance plane|policy snapshot|Proactive mode|listening"
```

预期（顺序不固定，关键字必须出现）：

```
INFO uc_grpc_server::governance_boot: Governance plane initialized durable=true
INFO uc_grpc_server::governance_boot: Installed governance policy snapshot version=2
INFO uc_grpc_server::governance_boot: Proactive mode configured mode="off"
INFO uc_grpc: Governance plane attached to the gRPC server durable=true
INFO uc_grpc_server: UltimateCoders gRPC server listening on 0.0.0.0:50051
```

> `durable=true` 才是持久化。若显示 `durable=false`，说明 `UC_GOVERNANCE_BACKEND` 不是 `postgres`
> （或 `UC_DATABASE_URL` 为空导致回退）——**重启后提交/case/mandate/审批全部消失**，这是本手册里最该先确认的一条。

### 1.2 路径 B：不用 Docker（nats-server + 二进制）

```bash
nats-server -js &                                   # 或已有 NATS：export UC_NATS_URL=nats://…
export UC_NATS_URL=nats://127.0.0.1:4222
export UC_GRPC_ADDR=127.0.0.1:50051
export UC_TASK_BACKEND=memory UC_EVENT_BACKEND=memory UC_SCHEDULE_BACKEND=memory
export UC_GOVERNANCE_BACKEND=memory                 # 端到端验收用内存即可；生产用 postgres
export UC_POLICY_BOOTSTRAP=dev                      # 仅本地：授予 operator 全部权限
export UC_OPERATOR_ID=operator
export UC_PROACTIVE_MODE=off                        # 先 off，第 6 节再打开
cargo run -p uc-grpc-server &                       # 首次编译较慢
# worker（另开终端，同样的 UC_NATS_URL / 网关地址）
python -m ultimate_coders.nats_worker               # 或 ./run-cluster.sh --workers 1
```

> 内存治理后端下 `durable=false`，**只能用来验证行为，不能用来验证持久化**。第 7 节给出持久化验证。

---

## 2. 桥是否活着（最先做，后面的失败都可能是它）

```bash
python - <<'PY'
import asyncio, os, json, nats
from ultimate_coders.governance_client import GovernanceClient

async def main():
    nc = await nats.connect(os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222"))
    client = GovernanceClient(nc)
    print(json.dumps(await client.call("policy.current", {}), ensure_ascii=False))
    await nc.close()

asyncio.run(main())
PY
```

预期：返回一个 JSON（`version` / 授权条目数量），**不是** `GovernanceUnavailable`。
失败时：

| 现象 | 原因 |
| --- | --- |
| `GovernanceUnavailable` | 网关没起，或 `UC_NATS_URL` 与网关不一致，或 NATS 不可达 |
| `{"ok": false, "error": {"code": "unknown_op"}}` | 网关是**旧版本**（没有治理桥）；先升级网关 |
| 超时 | NATS 通但网关的 `spawn_nats_bridge` 没跑起来（看网关日志是否有 `governance NATS bridge listening`） |

---

## 3. PR01 可靠提交（一次提交 = 一个逻辑 task）

```bash
python - <<'PY'
import asyncio, json, os, nats
from ultimate_coders.governance_client import GovernanceClient

async def main():
    nc = await nats.connect(os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222"))
    client = GovernanceClient(nc)
    payload = {
        "description": "e2e: prove submit-once",
        "project_id": "e2e-project",
        "principal_kind": "user",
        "principal_id": os.environ.get("UC_OPERATOR_ID", "operator"),
        "submission_id": "e2e-pr01-once",
    }
    first = await client.call("submission.submit", payload)
    second = await client.call("submission.submit", payload)
    print("first :", json.dumps(first, ensure_ascii=False))
    print("second:", json.dumps(second, ensure_ascii=False))
    await nc.close()

asyncio.run(main())
PY
```

判定：

- 两次的 `submission.task_id` **相同**；
- 第二次 `dedup_replayed: true`；
- 第二次**没有**新建 task（`submission.state` 仍指向同一条记录）。

反例（必须拒绝）：换 `description` 但沿用同一个 `submission_id` → `success: false`，错误里说明该 id 已用于
不同请求（内容冲突），**不会**覆盖原记录。

---

## 4. PR04 触发 → case（去重、唤醒、恢复）

`delivery_id` 只做审计，不参与身份。把**同一个事件**投两次：

```bash
python - <<'PY'
import asyncio, json, os, nats
from ultimate_coders.governance_client import GovernanceClient

EVENT = {
    "source": "github", "kind": "pull_request_comment",
    "external_id": "e2e-comment-1", "thread_key": "acme/widgets#7",
    "repo": "acme/widgets", "actor": "alice",
    "payload_json": json.dumps({"body": "please fix"}),
    "open_case_if_unmatched": False,
    "principal_kind": "service", "principal_id": "dashboard",
}

async def main():
    nc = await nats.connect(os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222"))
    client = GovernanceClient(nc)
    first = await client.call("trigger.ingest", dict(EVENT, delivery_id="d-1"))
    second = await client.call("trigger.ingest", dict(EVENT, delivery_id="d-2"))
    print("first :", json.dumps(first, ensure_ascii=False))
    print("second:", json.dumps(second, ensure_ascii=False))
    await nc.close()

asyncio.run(main())
PY
```

判定：`dedup_key` 两次相同；第二次 `duplicate: true`、`accepted: false`，且**不会**新建第二个 case
（`matched_cases` 为空正是重复投递的形态）。

**唤醒 + 恢复（opt-in 才有）**：需要一个停在 `waiting_event` 且有 `dispatch` 权限 mandate 的 case，
其订阅带 `auto_resume`。用 RPC 化的最省事方式：`case.create`（`issue_mandate` + 含 `Action:dispatch` 的
scopes）→ `case.transition` 到 `waiting_event` → `trigger.subscription.upsert`（`auto_resume: true`）→
再投递一次新事件。判定：`trigger.ingest` 的回复出现 `resumed_task_ids`（恰好 1 个），`case.get` 显示
`status: active` 且 `runs` 多了一条、其 `task_id` 与 `resumed_task_ids[0]` 一致。

---

## 5. PR03/PR05 权限与容量

### 5.1 未授权扩缩容被拒

```bash
python - <<'PY'
import asyncio, json, os, nats
from ultimate_coders.governance_client import GovernanceClient

async def main():
    nc = await nats.connect(os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222"))
    client = GovernanceClient(nc)
    try:
        print(await client.call("policy.authorize", {
            "permission": "worker_scale", "tool": "scale_workers",
            "principal_kind": "service", "principal_id": "dashboard",
        }))
    finally:
        await nc.close()

asyncio.run(main())
PY
```

判定：`allowed: false`，`deny_code` 为 `missing_permission` 或 `forbidden_principal_kind`。
（`service:dashboard` 在 dev 快照里**没有** `worker_scale`——这是「不越权」的预期，不是 bug。）

### 5.2 容量来自实时注册表

```bash
# 两个 worker 已注册（第 1 节），看容量
python - <<'PY'
import asyncio, json, os, nats
from ultimate_coders.governance_client import GovernanceClient

async def main():
    nc = await nats.connect(os.environ.get("UC_NATS_URL", "nats://127.0.0.1:4222"))
    print(json.dumps(await GovernanceClient(nc).call("capacity.get", {}), ensure_ascii=False))
    await nc.close()

asyncio.run(main())
PY
```

判定：`worker_slots_total` 等于两个 worker 的 `max_capacity` 之和（**不是** `UC_WORKER_SLOTS_TOTAL`）；
`busy = Σ min(current_load, max_capacity)`；`slots_saved_by_quiet_waiting == waiting_event_cases + verifying_cases`。

### 5.3 人工审批回路

```bash
# a) 申请（agent 身份；治理层会 raise 一条 needs_human）
# b) 用 operator 身份批准（需要 user + proactive_execute）
# c) 再 authorize 一次 tool=scale_workers → 这次 allowed，且许可被消费（状态 used）
```

判定：批准后第一次 `authorize` 放行；**第二次立即恢复拒绝**（单次许可）。
用 `approval.list?state=used` 能看到 `used_at`。若批准返回 `missing_permission`，说明当前 principal 不是
`user` 或没有 `proactive_execute`——审批**不接受** service/agent 代答。

---

## 6. PR06 watchdog 与提案（默认关闭）

```bash
export UC_PROACTIVE_MODE=observe      # 先观察：只记录，不提交
export UC_PROACTIVE_MODE=propose      # 再打开提案
```

制造一次停滞（让某个 subtask 连续 2 次以上无新证据，例如让测试一直失败且改动不变），然后看：

| 检查 | 期望 |
| --- | --- |
| 网关日志 | `watchdog stall report decided` 带 `verdict=…` |
| `attention.list` | verdict 为 `escalate_needs_human` 时出现一条 `needs_human`（网关服务端 raise） |
| worker 事件 | 失败事件里 `stall_signal.verdict` 与 `verdict_source: gateway` |
| `proactive.proposal.list` | `propose` 模式下出现一条 `unblock_stalled` 提案（`observe` 模式**不出现**） |

`UC_WATCHDOG_BRIDGE=off` 可关掉 worker 侧的询问；关掉后行为与接线前完全一致。

> worker 的提案身份是 `agent:uc-worker`。dev 快照只给 operator `propose` 权限，所以本地会看到
> `proactive_propose` 的拒绝日志——这是预期；要真的落库，用
> `UC_POLICY_GRANTS_JSON` 给该 agent 授权（见 `docs/governance/operations.md`）。

---

## 7. 持久化与恢复（必须用 postgres 后端）

```bash
export UC_GOVERNANCE_BACKEND=postgres
export UC_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/uc
./run-gateway.sh down && ./run-gateway.sh up --docker     # 重启网关
# 再查：policy.current / submission.get / case.get / approval.list 都应保留
```

判定：重启前建的 case 与审批记录仍在，且 `durable=true`；`governance_records` 表存在且行数 > 0
（`psql -c "select namespace, count(*) from governance_records group by 1"`）。

---

## 8. 清理

```bash
docker compose -f docker/docker-compose.yml --profile worker down
./run-gateway.sh down --docker
```

---

## 9. 常见失败与判定表

| 现象 | 最可能的原因 | 怎么确认 |
| --- | --- | --- |
| 所有治理 op 都 `GovernanceUnavailable` | 网关没起 / NATS 地址不一致 / 桥没跑 | 网关日志有无 `governance NATS bridge listening` |
| `unknown_op` | 网关是旧版本 | `git log -1` 与 `policy.current` 是否能返回 |
| 一切 `missing_permission` | 策略快照为空或未给当前 principal 授权 | `policy.current` 看 `version` 与条目；本地用 `UC_POLICY_BOOTSTRAP=dev` |
| `tool_requires_human_approval` | human-tier 工具且没有 operator 权限/审批 | 走第 5.3 节；或给 principal `proactive_execute` |
| `durable=false` | 治理后端不是 postgres / 连接串为空 | 网关启动日志第一行 `UC_GOVERNANCE_BACKEND=…` |
| 提交没有产生 task | 引擎/调度后端不可用 | 看 `submission.get` 的 `state`，再看网关日志的 outbox 投递 |
| worker 提案被拒 | `agent:uc-worker` 没有 `proactive_propose` | `policy.current`；或 `UC_POLICY_GRANTS_JSON` 授权 |
| `401` 来自仪表盘 | 部署要求 `UC_DASHBOARD_TOKEN` 且浏览器没有 token | 重新登录，或用 `Authorization: Bearer …` 调 API |
