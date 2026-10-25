# Hy-Memory 认知记忆后端

Hy-Memory（腾讯混元，Apache-2.0，<https://github.com/Tencent-Hunyuan/Hy-Memory>，PyPI: `hy-memory`）
是 UltimateCoders 的**认知记忆后端**：它从编码 Agent 的工具调用轨迹中自动抽取
事实与可复用经验，并在 `coding` 场景下维护一条**代码版本演化链**（diff chain）——
“最终保留了哪个实现”这类问题可以跨会话回答。

它与仓库既有的记忆层分工明确，不重复造轮子：

| 层 | 存放内容 | 写入方式 |
|---|---|---|
| `ultimate_coders.memory`（ShortTerm / LongTerm） | Agent **显式写入**的 KV 与语义检索 | `engine.read_memory/write_memory/search_memory` |
| **Hy-Memory（本文档）** | 从轨迹**自动抽取**的 facts / experiences + 版本演化链 | `CodingMemory.remember()` / MCP 工具 |

## 概念表

| 概念 | 值 / 说明 |
|---|---|
| SDK 包 | `hy-memory`（可选依赖，未安装时全部入口优雅降级） |
| 版本底线 | `2.0.0`（coding 场景 + 版本演化链自此引入） |
| 默认模式 | `pro`（LLM 抽取 + reconcile；`lite` 忽略 scene 只存原文，`ultra` 强制开图） |
| 场景 | `coding`（软件工程：facts + experiences + 版本链） |
| 隔离坐标 | `user_id`（一级）/ `workspace_id`（仓库，建议 git remote 规范化）/ `branch` |
| 来源标记 | 所有写入带 `source="ultimate-coders"`，召回可按来源过滤 |
| 存储 | ChromaDB 向量（默认）+ 本地 SQLite 元数据；图库可选（Kuzu / Neo4j） |
| LLM / Embedding | OpenAI 兼容协议——DeepSeek / DashScope / OpenAI 均可 |

**这不是一个编码 Agent**：hy-memory 不注册进 Agent 插件注册表
（`UC_CODING_AGENT` 没有它的取值），任何 Agent 都可以使用它。

## 安装

```bash
pip install "hy-memory==2.0.2"    # 核心（含 Chroma 与 Kuzu）
```

 worker 镜像未预装；本仓库 agent stack 对它是 import-guarded 的可选依赖。

## 配置（环境变量）

SDK 全部走 OpenAI 兼容端点，官方推荐配置即 DeepSeek：

```bash
export MEMORY_LLM_API_KEY="sk-..."                          # 必填（或 OPENAI_API_KEY）
export MEMORY_LLM_BASE_URL="https://api.deepseek.com"       # 推荐
export MEMORY_LLM_MODEL="deepseek-chat"

export MEMORY_EMBEDDER_API_KEY="sk-..."                     # 必填（或 OPENAI_API_KEY）
export MEMORY_EMBEDDER_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export MEMORY_EMBEDDER_MODEL="text-embedding-v3"
export MEMORY_EMBEDDING_DIMS=1024

export MEMORY_MODE="pro"                                    # lite / pro / ultra
export MEMORY_DATA_DIR="/data/hy-memory"                    # 本地存储根
```

> `MEMORY_LLM_API_KEY` 与 `MEMORY_EMBEDDER_API_KEY` 可同时由单个 `OPENAI_API_KEY` 覆盖——该兜底在**进程内路径**（doctor / preflight / `CodingMemory` 直接读宿主 `os.environ`）生效。
> **沙箱内例外**：`OPENAI_API_KEY` 在本仓库是 adapter 级凭证（仅 codex / local-harness），不会经共享白名单下发；MCP 工具在沙箱里只能看到 `MEMORY_*` 名字，需要单 key 姿态时显式设置两个 `MEMORY_*` key（compose override 转发的正是这批名字）。
> MCP 工具的默认隔离坐标另有 `UC_MEMORY_USER_ID` / `UC_MEMORY_WORKSPACE` / `UC_MEMORY_BRANCH`。

## 进程内使用

```python
from ultimate_coders.agent.hy_memory import CodingMemory

mem = CodingMemory(
    user_id="jameryw",
    workspace_id="github.com/JameryW/UltimateCoders",
    branch="feat/hy-memory",
)

# 任务结束后：写入一条含工具调用的轨迹
result = mem.remember(messages)          # {"success": True, "fact_ids": [...], ...}

# 纠正旧事实时：标记取代，绝不删除（D6 / T11）
result = mem.remember(
    messages,
    supersede_fact_ids=["fact-old-id"],
)
# metadata 带 supersedes + supersede_policy="mark_not_delete"
# 返回含 superseded_ids；只走 client.add，从不 delete/remove/drop

# 新任务开始前：召回事实 / 经验 / 版本链
hits = mem.recall("search 超时最终用的哪个实现？")
for fact in hits["facts"]:        ...
for exp in hits["experiences"]:   ...
hits["chain_block"]               # 版本演化上下文

mem.close()
```

`recall()` 永不抛异常：端点不可用时返回 `ok=False` + 空列表，调用方记录即可。

**写入硬约束（supersede-not-delete）**：`CodingMemory.remember` 与 MCP
`remember_coding_memory` 对“更新”语义只标记旧事实为被取代
（`supersedes` + `supersede_policy=mark_not_delete`），**从不**调用
Hy-Memory 客户端的 delete / remove / drop。与 `UC_HY_MEMORY_AUTO_CONTEXT`
自动注入（T12）无关——该开关默认关，本路径不引入注入。

## MCP 工具（沙箱内 Agent）

`ultimate_coders.agent.engine_mcp` 在 hy-memory 可用时额外暴露两个工具
（`mcp` 包或 `hy-memory` 包缺失时自动隐藏）：

| 工具 | 用途 |
|---|---|
| `recall_coding_memory` | 按 query 召回 facts / experiences / 版本链，支持 `workspace_id` / `branch` / `limit` 覆盖 |
| `remember_coding_memory` | 写入一条含工具调用的消息轨迹；可选 `supersede_fact_ids`（标记取代，不删除） |

沙箱内的 MCP 子进程通过 `SHARED_ENV_ALLOWLIST` 拿到 `MEMORY_*` 变量
（与 `UC_*` 控制面同级，见 `python/ultimate_coders/agent/sandbox.py` 的
`_HY_MEMORY_ENV_ALLOWLIST` 单例化元组）。

## Doctor（启动诊断，无 API 费用）

```bash
python -m ultimate_coders.agent.hy_memory --check     # 人读
python -m ultimate_coders.agent.hy_memory --json      # 机读（密钥脱敏）
```

检查链：包已安装 → 版本 ≥ 2.0.0 → LLM/Embedder 端点已配置 → 客户端可构造并关闭。
注意 SDK 的 `HyMemoryClient(...)` **无凭证也能构造成功**，失败只发生在
`add()`/`search()` 时——所以 preflight 检查端点“存在性” + 一次构造循环，
不做任何付费调用。

## Docker 部署

```bash
cp docker/.env.hymemory.example docker/.env   # 填入两个 key
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.hymemory.yml \
  --profile app up --build
```

override 只做三件事：给 `orchestrator` / `worker` / `nats-worker` 转发
`MEMORY_*` 变量、挂载命名卷 `hymemory_data:/data/hy-memory`（记忆跨重启持久化）、
**不钉任何 Agent**（与 dsec 的本质区别：hy-memory 是后端不是 Agent）。
与 `docker-compose.dsec.yml` 可叠加——DeepSeek 端点同时充当记忆后端的 LLM 提供方。

## 安全姿态

- **deny-by-default 延续**：`MEMORY_*` 走 `SHARED_ENV_ALLOWLIST` 窄名单
  （10 个精确名，无 `*` 通配），主机侧其他秘密不会漏进沙箱。
  `OPENAI_API_KEY` 不在其内——它是 adapter 级凭证，见上文配置节。
- **doctor 脱敏**：密钥只显示 `sk-xxx…1234` 形态（`_mask_key`）。
- **`--json` 日志静默**：SDK 首次构造客户端时会把 INFO 打到 stdout，
  `--json` 路径先 `MEMORY_LOG_LEVEL=ERROR`（`setdefault`，运维显式设置优先）。

## 排障表

| 症状 | 根因 | 处置 |
|---|---|---|
| doctor: `hy-memory is not installed` | 可选依赖未装 | `pip install "hy-memory==2.0.2"` |
| doctor: `older than the required 2.0.0` | 版本过低（1.x 无 coding 场景） | `pip install -U hy-memory` |
| doctor: `endpoints are not configured` | 缺 `MEMORY_LLM_API_KEY` / `MEMORY_EMBEDDER_API_KEY`（或 `OPENAI_API_KEY`） | 设置任一形式的 key |
| `recall()` 返回 `ok=False` | 端点不可达 / key 无效 / 数据目录不可写 | 看 `error` 字段；`--check` 复诊 |
| MCP 工具不出现 | `mcp` 或 `hy-memory` 包缺失 | 安装对应包后重启 MCP server |
| 记忆写入后搜不到 | `scene` 非 coding（无工具消息会回落 chat 链） | 传入含 `tool_calls` / `role="tool"` 的轨迹 |
| `lite` 模式下没有 facts | `lite` 忽略 scene，只做纯 embedding | 生产用 `pro`（或 `ultra`） |
| 容器重启后记忆丢失 | `MEMORY_DATA_DIR` 未落在 `hymemory_data` 卷上 | 用 override 默认值 `/data/hy-memory` |

## 测试映射

| 契约 | 测试 |
|---|---|
| 可用性/版本底线/掩码 | `tests/python/test_hy_memory_integration.py`（`test_available_matches_version`、`test_version_floor_comparisons`、`test_mask_key_never_leaks_the_middle`） |
| remember 传参（scene/坐标/source） | `test_remember_passes_coding_scene_and_isolation_coordinates` |
| supersede-not-delete（metadata / 禁 delete / MCP） | `tests/python/test_hy_memory_supersede.py` |
| recall 归一化 + 失败降级 + 空通道 | `test_recall_normalises_channels`、`test_recall_survives_client_failure`、`test_recall_tolerates_empty_channels` |
| 惰性客户端 / 关闭语义 | `test_lazy_client_built_once_and_closed_only_when_owned`、`test_injected_client_is_never_closed_by_the_wrapper` |
| preflight 全矩阵 | `test_preflight_*`（无包 / 低版本 / 缺端点 / 仅缺 embedder / OPENAI 兜底 / 构造失败） |
| doctor 脱敏 + CLI | `test_doctor_report_masks_keys`、`test_cli_*` |
| env allowlist 单例 + 行为 | `test_allowlist_tuple_shape`、`test_shared_allowlist_contains_every_memory_name`、`test_build_child_env_forwards_memory_vars_and_blocks_decoys` |
| MCP 门控 + 渲染 + 覆盖 | `test_memory_tools_*`、`test_memory_backend_*`、`test_mcp_*` |

真实 SDK 契约（`hy-memory==2.0.2`，add/search 的返回形状、无凭证可构造、
stdout 日志行为）在开发期以 mock OpenAI 兼容端点做过端到端验证；
测试套件本身不依赖 SDK 安装。
