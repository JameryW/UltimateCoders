# PRD: Python 审计收尾（#9/#11/#15）

## 背景

/loop 第 27 轮。Python 审计剩最后 3 项，清完 → 审计全清零。

## 清单（已核实）

### F64: file change 广播无大小上限（审计 #9）

`_broadcast_file_changes`（worker.py:1537+）全量 `fh.read()` 嵌 NATS 事件——sandbox 允许 500MB 文件 → 内存尖峰；payload >NATS max_payload（默认 1MB）→ publish 抛异常**仅 debug 吞** → gateway 静默不重索引该文件。

修：读前 `os.path.getsize` 检查，>512KB（1MB 上限留 margin）→ content 发 marker 串（gateway 可辨识 vs 空/删除）+ WARNING；publish 失败 debug→WARNING 带文件路径。

### F65: JetStream replay 死码（审计 #11，LOW-MED）

两处错：(1) consumer 创建带 `deliver_subject="uc.task.event.replay"` → PUSH consumer，但 `_replay_missed_events` 用 `pull_subscribe(durable="dashboard-replay")` 绑它 → 服务端报错（pull 不能绑 push consumer），整个 replay 初始化失败被 warning 吞；(2) `msg.sequence` 属性不存在（nats-py 应 `msg.metadata.sequence.stream`）→ `_js_last_seq` 永不前进。净效果：每次重启跳过 replay（仅 snapshot 对账兜底）。

修：consumer 创建去 `deliver_subject`（→ pull consumer，与 pull_subscribe 匹配）；sequence 改 `msg.metadata.sequence.stream`。

### F66: PTY 线程泄漏 + SearchQuery 健壮性（审计 #15，LOW）

(a) `_pty_reader`（app.py:844+）`run_in_executor(None, os.read)`——os.read 阻塞的线程**不可 cancel**，每断连客户端永久停一个默认 executor 线程。修：fd 设 O_NONBLOCK + 无数据时 `await asyncio.sleep(0.02)`——coroutine 停在 sleep await 点，cancel 可达，线程不占。
(b) `SearchQuery.limit()` 无校验（负数/巨值透传）→ clamp 1..1000；`in_all_repos` 的 `list_repos` 失败静默 `pass`（搜索范围静默放宽到全仓库）→ 改 warning 日志。

## 验收

- 新测试：SearchQuery.limit clamp（负→1、巨→1000、正常不变）；in_all_repos 失败不抛（既有行为）+ 有日志。
- broadcast 大小阈值逻辑简单（getsize 分支）——现有 worker 测试不受影响；_pty_reader 依赖 PTY 难单测，靠模式审查（nonblock+sleep cancel 点是标准模式）。
- pytest tests/python 全绿 + ruff exit 0；feature branch + PR + CI green。

## 意义

Python worker 审计 15 finding **全部完成**。此后方向：Rust gRPC crates 审计（CLAUDE.md 架构核心，从未审计）/ dashboard 前端重审 / 停 /loop——需用户决策。
