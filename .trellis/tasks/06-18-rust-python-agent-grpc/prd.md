# 完善后端实现: Rust引擎+Python Agent+gRPC

## Goal

完善 UltimateCoders 后端三大层的实现：Rust 核心引擎、Python Agent 层、gRPC 服务端。

## What I already know

### Rust 核心引擎 (5 crates, ~30,000 lines) — 全部已实现，无 stub

- **uc-types** (1,099 lines): 完整类型/traits，EngineApi 14+ async methods
- **uc-engine** (19,384 lines): LocalEngine 完整实现，所有子系统有真实后端 + in-memory fallback
- **uc-grpc** (6,534 lines): 18 RPCs 全部实现，server + client + conversions + local_worker_bridge
- **uc-grpc-server** (90 lines): 独立服务器二进制，完整
- **uc-python** (2,771 lines): PyO3 绑定完整，sync + async 方法，双模式 (local/grpc)

### Python Agent 层 (~12,100 lines) — 全部已实现，有少量 placeholder

- **Orchestrator** (1,251 lines): 完整任务生命周期，双分解路径，容错
- **Worker** (1,255 lines): 双执行模式 (llm/sandbox)，12 tools
- **LLMClient** (363 lines): Anthropic tool-calling + retry
- **Engine** (931 lines): PyO3/gRPC 切换 + auto-fallback
- **SandboxManager** (713 lines): Claude Code, Codex, Decompose adapters
- **RateLimiter** (432 lines): token bucket + circuit breaker
- **ConflictDetector** (409 lines): intent-based locking
- **NatsWorker** (582 lines): NATS bridge
- **LocalWorker** (388 lines): JSON-RPC bridge
- **CodegraphClient** (388 lines): SQLite FTS5 + BFS
- **Scheduler** (303 lines): PyO3 wrapper + YAML config
- **Memory** (414 lines): ShortTerm + LongTerm wrappers
- **Dashboard** (1,054 lines): FastAPI + SSE
- **TUI** (477 + 314 lines): Textual terminal UI

### gRPC 服务端 — 18/18 RPCs 全部实现

- EngineService: 12 RPCs (含 SearchStream server-streaming)
- TaskService: 6 RPCs (含 WatchTask server-streaming)
- 三种任务执行后端: NATS → Local Worker Bridge → newline-split fallback

## Known Gaps (placeholder/TODO)

1. **ConflictResolver._llm_assisted_merge()** — 返回 `MergeResult(success=False)`，是 placeholder
2. **ConflictResolver._auto_merge()** — 简化三路 diff，冲突时生成 conflict markers 而非真正合并
3. **config.load_config()** — TODO: 从 TOML/YAML 文件加载，目前只读环境变量
4. **NatsWorker._execute_subtasks()** — 调用私有方法 `_select_next_subtask()`，应改用公共 API
5. **Worker._collect_modified_files()** — 只追踪 read_file/edit_file，diff 字段始终为空
6. **AgentAdapter 基类** — 无 ABC/@abstractmethod 强制约束
7. **CORS** — `AllowOrigin::Any`，需生产环境加固

## Assumptions (temporary)

- 用户想补全上述已知 gap，或有其他未覆盖的完善方向
- 可能涉及新增功能（如更多 LLM provider、更多 proto RPC、持久化改进等）

## Open Questions

- 具体想完善哪些方向？（已知 gap / 新功能 / 性能优化 / 测试覆盖 / 其他）

## Requirements (evolving)

* (待确认)

## Acceptance Criteria (evolving)

* [ ] (待确认)

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Out of Scope (explicit)

* (待确认)

## Technical Notes

* 所有 5 个 Rust crates 编译通过，零错误零警告
* Python 层依赖 anthropic/pyyaml 为 soft dependency（运行时 import）
* uc-grpc 依赖 uc-engine 时 default-features=false，避免拉入存储后端
- uc-grpc-server 是唯一启用 uc-engine 默认 features 的 crate
