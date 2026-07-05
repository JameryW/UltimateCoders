# Agent 实现逻辑全面检查与跨仓库修复

## 背景

全面审查 agent 实现代码（Python worker/agent 层 + Rust sandbox agents + TS orchestrator），
发现并修复潜在 bug，确保所有测试通过。基线测试全绿（py 555/0, ts 104/0, rust 491/0）。

## 范围

4 个并行 reviewer 扫描：worker.py、sandbox.py、merge_arbiter+conflict、Rust agents。
跨仓库检查：vendor/oh-my-pi 为 OMP submodule（仅 prompt .md，无 agent 代码），无跨仓库修复点。

## 已修复（验证为真实 bug）

### Python
1. **llm.py:540-590** — Anthropic tool-call loop 每个 tool_call 发独立 assistant+user 消息对。
   API 要求同一 turn 的所有 tool_use 在一个 assistant 消息、所有 tool_result 在一个 user 消息。
   修复：循环前收集 blocks，循环后一次 append。
2. **worker.py:667-689** — retry 路径释放 workspace 后复用 stale handle → double-release +
   agent 在已删除 worktree 运行。修复：每次 attempt 重新 acquire，release 后置 None。
3. **worker.py:1113** — abort_on_failure=False 链累积失败步骤的部分 file_changes；后续成功把
   subtask 标 success 但夹带脏数据。修复：仅累积成功步骤。
4. **sandbox.py:519** — `elapsed if 'elapsed' in dir() else 0`：dir() 返回属性名不返回局部变量，
   恒为 0。修复：直接用 elapsed（已绑定）。

### merge_arbiter.py
5. 并发 arbitrate() 在共享 clone 上竞争 → checkout/merge/push 交错损坏状态。修复：asyncio.Lock。
6. fetch subtask 分支 exit code 未检查 → 缺失分支被误报为 conflict。修复：检查 fetch 结果。
7. 非冲突 merge 失败（bad object/lock/identity）被升级到 ConflictResolver。修复：用
   `--diff-filter=U` 区分真冲突与基础设施错误。
8. `merge --abort` exit code 未检查 → broken merge state 污染后续分支。修复：检查 exit code，
   失败则 `reset --hard`。
9. `git push` 非 fast-forward 被拒丢工作。修复：`--force-with-lease`。
10. fetch refspec 非 fast-forward 被拒。修复：`+refs/heads/...` force-fetch。

## 非 bug（已验证跳过）

- Rust `claude_code.rs:175` 非 JSON stdout → success:true：现有测试
  `claude_code_parse_output_non_json` 断言这是预期契约（plain-text summary 路径）。跳过。

## 测试结果

修复后全绿：
- Python: 555 passed, 0 failed
- TypeScript: 104 passed, 0 failed
- Rust: 491 passed, 0 failed

## 跨仓库

vendor/oh-my-pi（OMP submodule）仅含 prompt .md 文件（decomposer/worker/supervisor），
无 agent 代码逻辑，无修复点。TS orchestrator agents/ 目录无测试覆盖但无代码 bug。
