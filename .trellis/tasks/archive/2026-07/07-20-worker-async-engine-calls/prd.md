# PRD: Worker Engine 调用异步化（#6 MED）

## 背景

/loop 第 24 轮，Python 审计 #6（MED/M）。`execute_subtask` 异步流里调**同步** engine API（gRPC 模式走 `_try_grpc_with_fallback` 阻塞 RPC，`grpc_timeout_seconds=30`）：`_build_search_context`（每 subtask 开局）、`_save_checkpoint`/`_load_checkpoint`（每 attempt）、shared memory 三件 + `search_code`。gateway 慢/卡 → worker **整个事件循环冻结最长 30s/次** → NATS 心跳（30s 间隔）失速、全并发 subtask 停滞 → 逼近 gateway 90s stale 阈值 → **假性驱逐**。

## 改

engine 四个 async 变体已存在（engine.py: search_async/read_memory_async/write_memory_async/delete_memory_async，kwargs 与同步版兼容）。

1. 模块级小 helper `_engine_call(engine, sync_name, async_name, *args, **kwargs)`：有 async 变体 → await（gRPC 非阻塞）；无 → 调同步（内存引擎/旧 mock 无 *_async，同步本来就快不阻塞）。一处定义，八处复用。
2. `_load_checkpoint`/`_save_checkpoint`/`_build_search_context` → `async def` + `await _engine_call(...)`；execute_subtask 内三调用点（L613/643/717）加 await。
3. 公共 API `search_code`/`read_shared_memory`/`write_shared_memory`/`delete_shared_memory` → `async def` + helper（**仓库内零调用方**——grep 核实，仅 docstring/日志引用；async 化零破坏，为未来异步调用方备好）。`_broadcast_memory_changed` 调用不变（fire-and-forget create_task，async 上下文内照常工作）。

## 验收

- 既有 execute_subtask 路径测试全绿（checkpoint/search 走 async 变体）。
- 新测试：mock engine 带 search_async/read_memory_async → `_build_search_context`/`_load_checkpoint` await 到 async 变体（sync 版零调用）；engine 只有同步版 → 回落同步（兼容）。
- pytest tests/python 全绿 + ruff clean（**exit code 验证**）。
- feature branch + PR + CI green。

## 不做

- engine_mcp.py 的 `_search_code`（MCP 工具路径，独立；审计未列）。
- #8 SSE fan-out（下轮）；#9-#15 杂项。
