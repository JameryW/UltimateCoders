# D3 — Attempt 补丁轮次不可变、按 attempt 身份幂等，符号定位 AST 优先

Status: decided
Decided: 2026-11-15
Spec: [spec-p1.md](../spec-p1.md)

## Question

同一条 subtask 重试链上，每轮 attempt 到底改了什么、失败在哪？这份记录应当可被
重放（NATS 至少一次投递、worker 重启、事件重放），还应当不被后写覆盖。另外
「哪一行 / 哪个符号被改了」是给重试视图和后续知识页用的，必须稳定、可测。

## Decision

**每轮 attempt 一条不可变记录，身份即 `AttemptRef.key()`；同键同内容幂等返回，
同键不同内容抛错；符号定位 AST 优先、行号回退，永不抛异常。**

1. **记录身份**：`AttemptRef.key()` = `task:subtask:dispatch:attempt`。其中
   `dispatch_attempt` 是派发层级（重派发），`attempt` 是同一派发内的重试层级；
   两者分开，避免不同层的重试撞键（`AttemptRef.order()` 按
   `(dispatch_attempt, attempt)` 排序）。
2. **不可变**：`AttemptRound` 是 frozen dataclass；`AttemptChainLedger.record()`
   写 `attempt_chain` 命名空间，`key()` = `AttemptRef.key()`。同键同内容
   （`_fingerprint()` 归一化后比较）返回已存记录；同键不同内容抛
   `AttemptChainConflictError`——**永不覆盖**。
3. **轮次内容**：`FilePatch(path, change_type, old_path, additions, deletions,
   diff, truncated, symbols)` + `base_revision` / `head_revision` / `outcome` /
   `summary` / `failure` / `test_verdict`。`changed_paths()` / `is_failure()` /
   `file_count` 是派生视图，不重复存。
4. **有界采集**：`parse_git_diff(text, max_files=None, max_diff_chars=None)`
   的默认上限来自 `rollout.py` 的 `UC_KNOWLEDGE_MAX_PATCH_FILES`（默认 20）与
   `UC_KNOWLEDGE_MAX_DIFF_CHARS`（默认 20 000）；每文件 diff 超预算时截断并置
   `truncated=True`；每轮符号最多 `MAX_ROUND_SYMBOLS = 32`。不做无界存储。
5. **采集只读**：worker 在 sandbox 返回后、workspace `release()` 之前调用
   `WorkspaceManager.capture_diff()`（`agent/workspace.py`）拿到补丁事实；
   该调用只跑 `git diff` / `git status --porcelain` / `git rev-parse`，**从不**
   stage、commit、reset。未跟踪文件合成 add patch，单文件 >512 KB 跳过并留占位
   标记；rename 保留目标路径。
6. **符号定位**：`locate_symbols(path, diff, *, source_text="", max_symbols=20)`：
   `.py` 用 `ast` 解析出 dotted symbol（class/def 嵌套）及其行区间，按 hunk 变更
   范围取**最内层**节点；其它语言或无源码时回退为 hunk 行区间（`kind="line"`）。
   任何解析错误都吞掉并返回空列表——采集失败不能拖垮 attempt。
7. **日志安全**：`FilePatch.summary()` / `AttemptRound.to_dict()` 只给路径、
   计数、截断标记与短摘要，不把完整 diff 打进日志或事件。
8. **读取有界**：`rounds(limit≤1000)` / `prior_rounds(limit)` / `latest()`；
   扫描上限 `_MAX_SCAN = 5000` 条，重试视图只取最近 `DEFAULT_MAX_ROUNDS = 5`
   轮（见 D6）。

## Why

- **重复失败度量需要可比的每轮事实**：只有「第 N 轮改了哪些文件/符号、
  base→head 是什么、失败文本是什么」结构化后，相邻轮次的失败签名才可比
  （指标「重复失败签名率」）。
- **幂等是因为传输层不保证一次性**：NATS 至少一次 + worker 重启重放，如果
  `record()` 是插入或覆盖，会得到重复证据或丢失历史；同键幂等 + 冲突抛错把
  「重放」和「真 bug」分开。
- **冲突要响**：同键不同内容意味着 attempt 身份被复用或上游改了语义——静默
  覆盖会让指标与审计同时失真，这是必须炸出来的情况。
- **AST 优先是因为行号会漂**：同一符号在后续轮次行号变化很快；dotted symbol
  能跨轮次对齐「改的是同一个函数」，是 P3 恢复视图与 P4 知识页的共同键。
- **只读采集是硬约束**：采集绝不能成为副作用来源，否则「证据」本身进入了
  MergeArbiter 的仲裁范围（§8 不变量：副作用只经 effect ledger）。

## Boundary (what this decision does not do)

- 不判断补丁对错，也不计算失败签名（P2 的 `FAILURE_SIGNATURE` 证据才做）。
- 不存完整文件内容，不存 git 对象；`diff` 是有界文本，非可重放补丁。
- 不做跨 worker / 跨任务共享：`attempt_chain` 命名空间只按 task/subtask 读。
- 不覆盖 JS/TS/其它语言的 AST 解析：本阶段只 Python 有符号级定位，其余行号回退。
- 不替代 Decision Record：轮次记录的是「补丁事实」，路由与终态审计仍在
  `decision_record`。
- 不做保留期清理策略（`clear()` 仅供任务收尾/测试调用）。

## Implementation ticket

Patch Event Adapter + 账本：T04 → [spec-p1.md](../spec-p1.md)（
`tickets/04-attempt-diff-chain.md`）；词汇复用 [D4](D4-evidence-claim-separation.md)
的 `AttemptRef`。
