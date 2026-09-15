# T11 勘察笔记

## 现状：唯一收口点

`python/ultimate_coders/agent/sandbox.py::SandboxManager._execute_subprocess`
（line 451）是全部 agent 子进程的**唯一** spawn 点：

- 调用者：`execute()`（line 362，纯 Python 分支，流式 `on_stdout_line` 与非流式
  `communicate()` 两条子路径都在函数内）、`execute_decompose()`（line 449）。
- **引擎分支实际是死的**（实证）：`execute()` line 348 的
  `self.engine.execute_in_sandbox(...)` 由 `hasattr` 守卫，而 `uc-python` 根本不向
  Python 暴露 sandbox 桥（`crates/uc-python/src/` 无 `create_sandbox` /
  `execute_in_sandbox`，`grep sandbox` 零命中；Rust 侧实现存在于
  `crates/uc-engine/src/local.rs:475/486` 但无 pyo3 包装）。⇒ 今天是**纯 Python 路径**
  在跑，`_execute_subprocess` 确实是唯一 spawn 点（票面判断成立）。
  此外 `execute_decompose` 全仓零调用者（仅定义），保留为 API 面。
- 引擎分支 `execute()` line 348：`self.engine.execute_in_sandbox(handle_id=…,
  **exec_request)`——`hasattr` 守卫，且**不经过** `_execute_subprocess`。
  ⇒ 本次改动不触碰该路径，也就**不能**往 `exec_request` 里塞新键（会把 TypeError
  抛进 pyo3 命名参数）。这是"不在请求 dict 里塞 agent"的直接原因。
- 现有 env 构造（line 490-492）：
  ```python
  # Build environment
  env = dict(os.environ)
  env.update(env_vars)
  ```
  `env_vars = request.get("env_vars", {})`（line 473）。

## 最近改动留下的既有形态

- `_execute_subprocess` 已有 `cancel_key` 参数从 request 读（T7 #643），说明
  "request 携带元数据 + 显式参数"两种风格在本函数内并存，加 `agent` 参数不突兀。
- 测试直接调用 `manager._execute_subprocess(request)`（test_sandbox.py:581/618，
  位置传参），新参数必须带默认值。

## agent 身份如何取到

- `AgentAdapter.name()` 是**方法**（line 654 `@abstractmethod def name(self) -> str`），
  返回规范名，且与 registry 的 `AgentPluginSpec.name` 一致：
  - GrokBuildAdapter → `"grok-build"`（line 1081）
  - ClaudeCodeAdapter → `"claude-code"`
  - DecomposeAdapter → `"claude-code-decompose"`（line 676）
  - CodexAdapter → `"codex"`
  - DeepSeekHarnessAdapter → `"deepseek-harness"`（别名 `deepseek`）
  - LocalLoopHarnessAdapter → `"local-harness"`（别名 `local-llm`）
- `execute()` 已有 per-call 覆盖：`adapter = self._create_adapter(agent) if agent
  else self._adapter`（line 334）⇒ 权威身份应取 `adapter.name()`，而不是
  `config.agent`（别名 "grok" 与规范名 "grok-build" 会分叉）。

## 凭据现状：适配器走覆盖层，宿主走 allowlist

- `config._build_env_vars()`（line 120）用 `registry.api_key_env_for(self.agent)`
  把 `api_key` 映射到该 agent 的凭据变量名 → 进 `request["env_vars"]` 覆盖层。
  ⇒ 适配器自注入的凭据**不受**宿主过滤影响（覆盖层在过滤后叠加）。
  allowlist 的意义在于：宿主机 shell 里已存在的凭据/密钥是否过闸。
- 覆盖层里还有非凭据项，均为适配器自建、随覆盖层通行：`GROK_HOME`（line 1161）、
  `CODEX_HOME`（line 1515）、临时 MCP 配置路径等。
- `harness_deepseek` 转发宿主 `DEEPSEEK_BASE_URL`（line 77）——读**父进程** env
  后塞进覆盖层，故无需白名单项。
- `harness_local_loop` 的凭据是**子进程内**读的（line 308-316 读
  `OPENAI_DEFAULT_MODEL`/`OPENAI_API_KEY`/`UC_LLM_TIMEOUT`），且该插件
  `api_key_env=None` ⇒ 只能靠硬清单 + `UC_*` 前缀覆盖。

## 票面清单 vs 实际适配器（差异已并入 prd）

票面 per-adapter 列表未提 `deepseek-harness` / `local-harness`（两个 in-tree 插件）。
不补则二者宿主侧凭据静默丢失 ⇒ 作为"补齐遗漏"显式记入 prd，不作为静默扩张。

## 环境事实（本机）

- 本机 demo 子进程用 `sys.executable -c "print(json.dumps(dict(os.environ)))"` 回读；
  Windows 下 `SYSTEMROOT`/`SYSTEMDRIVE`/`COMSPEC`/`PATHEXT` 必须过闸否则 Python 自身
  与 `cmd.exe` 可能起不来（已入 base 组）。`TEMP`/`TMP` 票面未列——本机 Python 惰性
  建临时目录，不需要；如后续 CLI 需要，走 `UC_SANDBOX_ENV_EXTRA`（逃生舱本意）。
  **实证**：新增用例里的真实子进程（Windows 宿主、env 无 TEMP/TMP）全部 exit 0 并
  正常输出，说明缺 TEMP 不影响 Python 启动。

## 已知限制（有意保留，非缺陷）

1. **`PYTHONPATH` 不在清单内**：`local-harness` 以 `sys.executable -m
   ultimate_coders.agent.harness_local_loop` 起子进程。生产镜像里包是 pip 安装的
   （无需 PYTHONPATH）；但若某开发机靠 `PYTHONPATH` 才 import 得到包，过滤后该 agent
   的宿主级 PYTHONPATH 会丢失。缓解：`UC_SANDBOX_ENV_EXTRA=PYTHONPATH`（已文档化）。
   选择不默认放行的理由：PYTHONPATH 是子进程代码注入面，且 D11 清单未列它。
2. **`UC_SANDBOX_ENV_EXTRA` 值不校验**：运营方接口，`*` 通配与任意名字直接进白名单，
   allowlist 只保证"默认窄"，不保证"运营方不会放宽"——启动日志就是审计线索。
3. **Rust 侧 `to_engine_config().env_vars` 不经过滤**：那条路径今天不可达（无 pyo3
   桥，见上），若未来接上 engine 分支，需要在 Rust 侧复刻同一策略（作为后续票面）。
4. **`UC_*` 前缀默认放行**：控制面命名空间整体可信，运营方不得把宿主密钥放进
   `UC_*` 名字里（本仓 `UC_*` 均为配置项，无密钥语义；`UC_QDRANT_API_KEY` 亦用于
   远端服务，属设计内）。
