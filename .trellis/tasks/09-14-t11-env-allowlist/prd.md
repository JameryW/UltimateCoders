# T11: Sandbox env allowlist — deny-by-default 过滤 + per-adapter 清单（#653）

## 背景

D11 #648：`sandbox.py::_execute_subprocess` 今天把**完整宿主环境**灌进每个 agent
子进程——`env = dict(os.environ); env.update(env_vars)`（sandbox.py:490-492）。
worker 是跨主机部署的（UC_SCALE_HOSTS），宿主 env 形状不可假设；任何编排器/CI 宿主
上的无关密钥（云凭据、代币、内部服务 token）都会随每一次编码代理执行外泄到 CLI
子进程。这是升级计划 §21 allowlist 项的根因，也是唯一收口点：`execute()` 与
`execute_decompose()`、全部适配器（含外部插件）、流式与非流式两条分支都经过它。

不可简单改为 denylist：agent CLI 需要自己的凭据（ANTHROPIC_/OPENAI_/XAI_ 等），
一刀切会直接打断认证。因此采用 **deny-by-default allowlist + per-adapter 扩展**。

## Scope

1. **过滤函数落在唯一收口点**：`_execute_subprocess` 改为
   `env = self.config.build_child_env(os.environ, env_vars, agent=…)`。agent 身份
   解析顺序：显式参数 → `request["agent"]` → `self.config.agent`。`execute()` 传
   `adapter.name()`（适配器自报的规范名，别名/单次覆盖都会被规范化）；
   `DecomposeAdapter.build_request` 在请求里自带 `agent`（分解路径唯一入口
   `execute_decompose` 手里没有适配器实例）。
2. **清单落在 SandboxConfig**（票面用词）：模块级常量
   `BASE_ENV_ALLOWLIST` / `SHARED_ENV_ALLOWLIST` / `ADAPTER_ENV_ALLOWLIST`
   + 方法 `child_env_allowlist(agent)` / `build_child_env(...)` / `env_extra_names()`。
   - base：PATH/HOME/USER/SHELL/TERM/LANG/LC_*/TMPDIR/PWD + Windows 组
     SYSTEMROOT/SYSTEMDRIVE/COMSPEC/PATHEXT/USERPROFILE/APPDATA/LOCALAPPDATA/
     PROGRAMFILES/USERNAME/COMPUTERNAME。
   - shared：UC_*（控制面）、HTTP(S)_PROXY / NO_PROXY（大小写两形）。
   - per-adapter：claude-code & claude-code-decompose →
     ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL/ANTHROPIC_MODEL；
     codex → OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_DEFAULT_MODEL；
     grok-build → XAI_API_KEY/GROK_API_KEY；
     **补齐票面遗漏的 in-tree 插件**：deepseek-harness →
     DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL；local-harness → OPENAI_*（litellm 路由，
     该插件注册 `api_key_env=None`，仅硬清单能覆盖）。
   - **插件驱动兜底**：额外并入 `registry.api_key_env_for(agent)` 返回的凭据变量名
     （外部插件声明的 api_key_env 自动进白名单，避免插件生态静默失去凭据）；
     别名经 `registry.get_spec().name` 归一。
   - `*` 后缀 = 前缀通配（LC_*/UC_*）。
3. **覆盖层纪律**：`env_vars`（适配器自建）在**过滤后的宿主环境**之上 add/override；
   它不构成旁路——宿主侧白名单外的变量不会因为覆盖层存在而回流。
4. **逃生舱**：`UC_SANDBOX_ENV_EXTRA`（逗号分隔，支持 `*` 后缀），
   `SandboxManager.__init__` 启动时若设置则 INFO 记录。
5. **测试**：`tests/python/test_sandbox_env_allowlist.py`——真实子进程回读环境
   （`json.dumps(dict(os.environ))`）验证：假想宿主密钥 SECRET_TOKEN 对所有适配器
   均不过闸；各 CLI 凭据过闸；覆盖层可加值；decompose 路径走 ANTHROPIC 清单；
   `UC_SANDBOX_ENV_EXTRA` 生效且被记录；LC_*/UC_* 前缀匹配。

## 验收（票面）

- 假想宿主密钥永远到不了任何 agent 子进程（全部适配器）。
- agent CLI 仍能认证（白名单内凭据过闸）。
- `UC_SANDBOX_ENV_EXTRA` 追加生效且被记录。

## 非目标

- Rust 侧 sandbox env（`to_engine_config` 的 env_vars 直传路径不动）。
- 敏感值脱敏/日志清洗；seccomp/网络隔离；denylist 兜底。
- 对 `UC_SANDBOX_ENV_EXTRA` 的语法校验（值直接进白名单，运维自负）。
