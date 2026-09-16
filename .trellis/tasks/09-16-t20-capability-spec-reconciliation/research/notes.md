# T20 #671 — 侦察笔记

所有结论都附**可复算的命令**。凡「某某已存在/已被删除」都有一条命令输出背书（本仓铁律）。

## §1 逐符号命中普查

```bash
for sym in _self_evaluate _classify_error _adaptive_retry _select_worker \
           schedule_subtasks _gather_prior_context _derive_capabilities \
           _resolve_agent_config _merge_agent_config; do
  n=$(git grep -c "$sym" -- python packages crates 2>/dev/null | wc -l)
  f=$(git grep -l "$sym" -- python packages crates 2>/dev/null | tr '\n' ' ')
  printf '%-24s 命中文件数=%s  %s\n' "$sym" "$n" "$f"
done
```

输出：

```
_self_evaluate           命中文件数=0
_classify_error          命中文件数=0
_adaptive_retry          命中文件数=0
_select_worker           命中文件数=0
schedule_subtasks        命中文件数=0
_gather_prior_context    命中文件数=0
_derive_capabilities     命中文件数=3  packages/uc-orchestrator/src/orchestrator/orchestrator.ts python/ultimate_coders/agent/registry.py python/ultimate_coders/agent/worker.py
_resolve_agent_config    命中文件数=4  crates/uc-grpc/src/server.rs python/ultimate_coders/agent/types.py python/ultimate_coders/agent/worker.py python/ultimate_coders/agent/nats_worker.py
_merge_agent_config      命中文件数=1  python/ultimate_coders/agent/sandbox.py
```

⇒ 前六个是**幽灵**；后三个仍活。这条分界线决定了本票只动规范前半部分。

## §2 独立复算（防止 grep 单点失误）

`_select_worker` 用了**三种**互不相同的方法，结论一致：

1. `git grep -n "_select_worker" -- python packages crates` → 空。
2. `git grep -n "_select_worker"`（全仓 tracked）→ 只有 `.trellis/spec/backend/agent-capability-spec.md` 的 7 行。
3. Grep 工具，`path=python`，`_select_worker|schedule_subtasks|_gather_prior_context|_self_evaluate|_adaptive_retry` → `No matches found`。

⚠️ 方法 4（裸 `grep -rn ... .`）**未采用**：它会扫 `target/` 等构建产物，5 分钟未收敛，已终止。**记录它是因为它是错的工具选择**：全仓裸 grep 在本仓不是「更彻底」，而是「不收敛」。

## §3 移除提交与日期

```bash
git log --oneline -S "_select_worker" -- python
# 05ccb56 chore: remove Python Orchestrator, update architecture docs (#161)
git log --oneline -S "_self_evaluate" -- python
# ad931ec fix(dashboard): P1/P2一致性修复 + sandbox统一 + 事件管道优化 (#111)
git log -1 --format="%h %ad %s" --date=short 05ccb56   # 2026-06-26
git log -1 --format="%h %ad %s" --date=short ad931ec   # 2026-06-21
```

⇒ 移除是**刻意的**（提交信息明写 remove Python Orchestrator），约三个月前；规范从未跟进。这两条 `-S` 只列 HEAD 可达提交，故与「现在不存在」不矛盾。

## §4 §3 契约的死码取证

```bash
for sym in _record_experience FALLBACK_TOOL test_select_worker_capability_match \
           test_select_worker_fallback_load confidence_threshold experience_key; do
  f=$(git grep -l "$sym" 2>/dev/null | tr '\n' ' '); printf '%-36s %s\n' "$sym" "${f:-<无命中>}"
done
```

输出：

```
_record_experience                   .trellis/spec/backend/agent-capability-spec.md .trellis/tasks/archive/2026-06/06-21-llm-worker/prd.md
FALLBACK_TOOL                        .trellis/spec/backend/agent-capability-spec.md python/ultimate_coders/agent/types.py
test_select_worker_capability_match  .trellis/spec/backend/agent-capability-spec.md
test_select_worker_fallback_load     .trellis/spec/backend/agent-capability-spec.md
confidence_threshold                 <无命中>
experience_key                       <无命中>
```

`FALLBACK_TOOL` 进一步取证（它落在源码里，需要判定是「活」还是「孤儿」）：

```bash
grep -n -B3 -A12 "FALLBACK_TOOL" python/ultimate_coders/agent/types.py
git grep -n "FALLBACK_TOOL\|fallback_tool" -- python tests packages crates
```

- `types.py:61`：枚举成员 `FALLBACK_TOOL = "fallback_tool"`，与 `NONE` / `SHRINK_SCOPE` / `PURE_LLM` / `WAIT_RETRY` 同属一个「失败后如何自适应」的枚举。
- 第二条命令**全仓只有一个命中**，就是它自己的定义行。

⇒ **定义即孤岛**（零生产者、零消费者）。这比「函数被删」更隐蔽：符号查找会命中，令人以为它活着。

## §5 「默认能力」的两层结构与实跑

读码得到的候选 seed（`worker.py:448`）：

```python
caps = ["code", "search", "memory", "test", "decompose"]
```

实跑（**这才是事实**）：

```bash
PYTHONPATH=python .venv/Scripts/python.exe -c "
from ultimate_coders.agent.worker import Worker
w = Worker(worker_id='probe-default')
print('caps =', w.capabilities)
print('exact match to 5-element seed:', w.capabilities == ['code','search','memory','test','decompose'])
print('review present:', 'review' in w.capabilities)
"
```

输出：

```
caps = ['code', 'search', 'memory', 'test', 'decompose', 'grok-build', 'grok', 'claude-code', 'codex', 'deepseek-harness', 'deepseek', 'local-harness', 'local-llm']
exact match to 5-element seed: False
review present: False
```

`review` 缺失这一项**符合** D14/T16 的设计（独立性前提）；13 项则是 seed 之后又被追加。

派生来源（`worker.py:520-534`）：`ensure_builtin_plugins()` + `agent_registry.registry.capability_names(shutil.which)` —— 注释自陈 *"CLI agents advertise only when their binary is on PATH; API-backed harnesses (e.g. deepseek-harness) always advertise. New agents registered as plugins show up here without touching this method."*

⇒ 结论（写进规范与测试的口径）：

| 层 | 内容 | 可钉性 |
|---|---|---|
| base seed | `code, search, memory, test, decompose` | **固定、可绝对钉** |
| MCP/工具派生 | `mcp`, `mcp:<server>`, `codegraph`, `lsp` | 取决于 SandboxConfig |
| opt-in 旗标 | `browser` / `debug` / `review` | 取决于 env |
| 插件注册表派生 | `grok*`, `claude-code`, `codex`, `deepseek*`, `local-*` | **随 PATH 与注册表变化 ⇒ 不可等值钉** |

⇒ 死项与顺序：seed 是列表前 5 项，后续追加**不重排**（去重保序），故 seed 可被稳定观测。

## §6 现有测试的强度（为什么要升级）

`tests/python/test_sandbox.py:689`：`test_default_capabilities` 断言 `"code"/"search"/"memory"/"test" in caps` —— **成员资格，非等值**。⇒ 它**不会**在「有人从 seed 里删掉 `decompose`」或「有人把 `review` 加进 seed」时变红（后者另有 `test_worker_capabilities.py:151` 的 `not in` 覆盖，前者无人守）。

`tests/python/test_worker_capabilities.py:151-159` 的 docstring 仍写 *"The dispatch side has no exclusion primitive (research/notes.md §3)"* —— 自 T19（`workers_with_capabilities_excluding`）起为假。**注意这个 docstring 的其余部分仍是对的**（「独立性搭在能力门上」的推理仍成立），故只更失效的半句 —— 与 T19 改 `worker.py` 那处注释同一手法（T19 改了 `worker.py:440-451` 却漏了这里）。

## §7 CI 触发面（用于判断本票的验证口径）

```bash
ls .github/workflows/          # ci-dashboard.yml ci-python.yml ci-rust.yml ci-typescript.yml
grep -n -A14 "^on:" .github/workflows/*.yml
```

| workflow | paths |
|---|---|
| ci-python | `python/**`, **`tests/**`**, `pyproject.toml`, `dashboard/**` |
| ci-rust | `crates/**`, `Cargo.toml`, `Cargo.lock`, `docker/docker-compose.yml` |
| ci-typescript | `packages/uc-orchestrator/**`, `vendor/oh-my-pi/*` |
| ci-dashboard | `dashboard/**` |

⚠️ **纠正一条长期记忆**：此前记「`tests/**` 不触发任何 CI」。实测 `ci-python.yml` **列了 `tests/**`** ⇒ 改测试**会**跑 Python CI。正确的口径是：`docs/**`、`.trellis/**`、`.gitignore` 不触发任何 CI；**`tests/**` 触发 Python CI（且只触发它）**。

对本票的含义：本票同时动 `tests/python/**` 与 `.trellis/**` ⇒ **Python CI 会跑，Rust/TS/Dashboard 不会** —— 与「零 Rust/TS 改动」自洽，这本身就是一条可核对的证据（若 Rust CI 被触发，说明我改了不该改的东西）。

## §8 爆炸半径

```bash
git grep -ln "self-evaluat\|adaptive retry\|self-reflection\|_self_evaluate\|worker selection" \
  -- docs .trellis/spec CLAUDE.md README.md
# .trellis/spec/backend/agent-capability-spec.md
# .trellis/spec/backend/index.md
```

⇒ 全仓只有**两个文件**在讲这层（散文或符号）。范围完全受控，不需要普查 31 个 spec 文件。

## §9 一次自我更正（方法论）

见 prd「一次自我更正」小节：我从 `worker.py:448` 的源码字面量**推断**默认能力是 5 项并准备写等值断言，实跑得 13 项 ⇒ **若按推断落笔，测试会红，而我会去修一个没坏的东西**。教训与 T19 同源但方向相反：T19 是「信了过期规范」，本票差点是「信了源码片段当运行时事实」—— **两者都是把间接信号当事实**。判据不变：跑一次。
