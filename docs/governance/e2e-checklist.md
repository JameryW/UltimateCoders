# 治理面 E2E 验收：证据检查单（可转交执行）

> **用途**：这份文件不是「怎么跑」（那是 [e2e-runbook.md](e2e-runbook.md)），而是**跑的时候记录什么、
> 怎么判定通过、证据放哪、结论回填到哪**。目标是把「端到端验收」从一次口头确认变成一份可以交给别人执行、
> 结果可以被审计的工件。
>
> **状态（诚实，2026-11-05 更新）**：本机已执行并**通过** S1–S10，外加 Dashboard HTTP 面的冒烟（健康、容量、
> 注意力、case、审批申请/批准、CSV 导出、触发摄取、worker 列表），详见
> [e2e-runbook.md](e2e-runbook.md) §10 与 §10.1（§10.1 记录并已修复的闸口缺口：`scale_workers` 动作现在同时
> 检查权限与 human-tier 闸口）。**仍未执行**：S11 的容器清理（需要 Docker）。执行者请在 §3 里逐项写结论，
> 不要用一次部分执行去概括整张表。

## 0. 谁执行、执行多久

| 角色 | 需要的环境 | 时间 |
| --- | --- | --- |
| 执行者 | 有 Docker + Docker Compose v2 的机器（或路线 B：`nats-server` + 本地二进制）；**网关必须用当次源码 `cargo build -p uc-grpc-server` 构建**，旧二进制会以 `unknown_op` 伪装成桥故障 | 首次约 60–90 分钟，含起停与填表 |
| 复核者 | 能读证据包 | 10 分钟 |

最小可执行子集（时间不足时先跑这四步，覆盖最高的三个风险面）：**§2 桥自检、§3 可靠提交、
§5.3 审批回路、§7 持久化重启**。

## 1. 证据包结构

在仓库外建一个目录（不要提交进仓库），命名 `uc-e2e-<YYYY-MM-DD>/`：

```
uc-e2e-2026-11-04/
├── 00-env.txt                 # docker compose version、git rev-parse HEAD、uname -a、日期
├── 01-boot/gateway.log         # ./run-gateway.sh logs 的完整输出（脱敏后）
├── 01-boot/compose-ps.txt      # docker compose ps 的完整输出
├── 02-bridge/capacity.get.json
├── 03-submit/first.json        # 两次提交的完整回复
├── 03-submit/second.json
├── 04-trigger/first.json       # 两次投递的完整回复（含 dedup_key / duplicate）
├── 04-trigger/second.json
├── 04-trigger/case.after.json  # 唤醒/恢复后的 case.get
├── 05-policy/authorize-denied.json
├── 05-policy/capacity.json
├── 05-approval/request.json    # 申请 → 批准 → 再授权 三次回复
├── 05-approval/decide.json
├── 05-approval/authorize-after.json
├── 06-proactive/proposal.list.json
├── 06-proactive/attention.list.json
├── 07-persistence/after-restart.json
├── 07-persistence/psql-governance_records.txt
└── 99-verdict.md               # §3 的填好版本
```

命名约定：每个文件对应**一条检查项**；命令的输出原样保存，不做手工编辑（脱敏只删 token/key，
并在 `99-verdict.md` 里注明删了什么）。判定失败时，额外保存 runbook §9 对照表里对应的那一行。

## 2. 检查单

每行：**步骤**（runbook 的节号）→ **判定通过的标准** → **证据文件** → **它支撑哪条验收**。

- [ ] **S1 集群起来**（runbook §1）— 网关日志同时出现 `Governance plane initialized durable=true`、
      `Installed governance policy snapshot`、`Governance plane attached to the gRPC server`；
      `docker compose ps` 里网关与至少 1 个 worker 是 `running/healthy`。证据：`01-boot/`。验收：可持续
- [ ] **S2 桥活着**（§2）— `capacity.get` 返回带 `capacity` 的 JSON，而不是 `GovernanceUnavailable`；
      `policy.current` **不是**桥 op（用它测会得到 `unknown_op`）。若 `capacity.get` 仍报 `unknown_op`，
      先确认网关是当次源码构建的二进制，**否则后续全部不用测**。证据：`02-bridge/`。验收：可验证
- [ ] **S3 提交一次**（§3）— 同一 `submission_id` 两次提交返回**相同** `task_id`，第二次
      `dedup_replayed: true`；改用不同内容复用同一 id 时被拒。证据：`03-submit/`。验收：不重复
- [ ] **S4 触发去重**（§4）— 同一事件投两次：`dedup_key` 相同、第二次 `duplicate: true`，且不产生第二个 case。
      证据：`04-trigger/first.json`、`second.json`。验收：不重复
- [ ] **S5 唤醒 + 恢复**（§4 后半）— 停在 `waiting_event` 且订阅 `auto_resume: true` 的 case，在投递新事件后
      `resumed_task_ids` 恰为 1 个，`case.get` 显示 `active` 且 runs 多一条、其 `task_id` 与之一致。
      证据：`04-trigger/case.after.json`。验收：不重复 + 可持续
- [ ] **S6 未授权扩缩容被拒**（§5.1）— `policy.authorize` 对 `service:dashboard` + `worker_scale` 返回
      `allowed: false`，`deny_code` 为 `missing_permission` 或 `forbidden_principal_kind`。
      证据：`05-policy/authorize-denied.json`。验收：不越权
- [ ] **S7 容量来自实时注册表**（§5.2）— **必须先起至少一个 worker 并等它注册**；`worker_slots_total`
      等于已注册 worker 的 `max_capacity` 之和（不是 env 值）；`slots_saved_by_quiet_waiting` 等于
      `waiting_event + verifying` 计数。没有 worker 时它必然为 0：那只能证明「空集群读作 0」，不是本项通过。
      证据：`05-policy/capacity.json`。验收：少打扰
- [ ] **S8 审批闭环**（§5.3）— 分两级记录：**(a) 申请 + 批准**（带 `case_id` 的申请会让 `attention.list`
      出现 `needs_human: approve …`，`approval.decide` 返回 `approved`）；**(b) 单次消费**——用一个**低于
      操作者层级**的 service（例如只授 `worker_scale` 的 `service:resizer`）对同一 `case_id` 调
      `ScaleWorkers(action="deregister")`：第一次到达动作路径且 `approval.list?state=used` 出现 `used_at`，
      第二次 `PermissionDenied: tool_requires_human_approval`。`deregister` 分支不需要 Docker，所以 (b)
      现在可以端到端验完。证据：`05-approval/`、`08-gate/`。验收：不越权 + 可验证
- [ ] **S9 watchdog 升级**（§6）— `UC_PROACTIVE_MODE=observe` 时只记录、不产生提案；
      切到 `propose` 后出现一条 `unblock_stalled` 提案；verdict 为 escalate 时 `attention.list` 出现
      `needs_human`——**报告中必须带 `case_id`**，否则服务端只记 warn、不投影（本机实测）。
      worker 失败事件里的 `verdict_source: gateway` 需要 worker 进程，未起 worker 时记「未测」。
      证据：`06-proactive/`。验收：可验证 + 少打扰
- [ ] **S10 持久化重启**（§7）— 重启网关后 `capacity.get`、`case.get`、`approval.list` 仍返回重启前的数据，
      日志 `durable=true`；`governance_records` 表非空。**反向对照**：不配 postgres（`memory` 后端）重启后
      必须 `durable=false` 且数据全部消失——两条都记录，才说明持久化开关真的起作用。
      证据：`07-persistence/`。验收：可持续
- [ ] **S11 清理**（§8）— 容器与网络全部停止，无残留进程。证据：`01-boot/compose-ps.txt` 的收尾版本（可选）。

## 3. 结论回填（执行者填写）

把本表复制到证据包的 `99-verdict.md` 并填写：

| 验收 | 支撑步骤 | 结论（通过 / 失败 / 未测） | 证据文件 | 备注 |
| --- | --- | --- | --- | --- |
| 不重复 | S3、S4、S5 | | | |
| 不越权 | S6、S8 | | | |
| 可持续 | S1、S5、S10 | | | |
| 可恢复 | S3（重启后再提交一次）、S10 | | | |
| 少打扰 | S7、S9 | | | |
| 可验证 | S2、S8、S9 | | | |

任何一行「失败」都必须附上 runbook §9 的对照行（现象 → 最可能原因 → 怎么确认）。

## 4. 失败时怎么办

1. 不要重跑整份清单来「碰运气」：先按 runbook §9 定位，确认是**配置**（`UC_*` 变量、策略快照为空、
   `UC_DATABASE_URL` 缺失）、**版本**（网关旧于 worker 契约）还是**代码**。
2. 如果是代码缺陷：开一个 issue，附证据包路径与最小复现命令；不要只贴截图。
3. 如果是配置问题并且文档没写清楚：这就是文档缺陷——回来改 runbook 的对应节，并在这里补一行。
4. 每次复测都新建一个日期目录，**不要覆盖**上一次的证据包。

## 5. 执行完之后回填仓库

- 把 [e2e-runbook.md](e2e-runbook.md) 顶部的「未执行」状态改为「已执行：<日期>，结论 <一行摘要>」，
  并附证据包路径（若证据不便入库，写明存放位置与保留期限）。
- 若六条验收中有任何一条首次被端到端证实，在 [README.md](README.md) 的六行验收表对应行的「证据」列补上
  `（E2E：<日期>，见 e2e-runbook）`——注意该表受文档一致性守卫约束：新增的引用必须是真实存在的测试名或
  本文件里的路径，否则守卫会报错。
- 若发现新的失败模式，补进 runbook §9 的对照表。
