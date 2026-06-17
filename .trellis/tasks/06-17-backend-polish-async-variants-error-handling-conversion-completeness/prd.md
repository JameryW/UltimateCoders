# Backend Polish: async variants, error handling, conversion completeness

## Goal

完善 Rust 后端：PyO3 层补充 async variants、LocalEngine 中 unwrap() 替换为 expect()。

## Requirements

### 1. PyO3 async variants for new methods
- `batch_write_memory_async` — async version, returns coroutine yielding Vec<PyMemoryEntry>
- `list_repos_async` — async version, returns coroutine yielding Vec<PyRepoIndexState>
- `search_stream_async` — async version, returns coroutine yielding Vec<PySearchResultItem>（collect 模式）

### 2. LocalEngine unwrap → expect
- `semantic_indexer().unwrap()` → `.expect("semantic indexer required — use with_semantic constructor")`（3 处）

## Acceptance Criteria

* [x] batch_write_memory_async 在 Python 中可用
* [x] list_repos_async 在 Python 中可用
* [x] search_stream_async 在 Python 中可用
* [x] LocalEngine 中无裸 unwrap()（3 处替换为 expect）
* [x] cargo clippy + fmt + test 全绿
* [x] CI green (PR #58)

## Out of Scope

* Python async generator for search_stream（保持 collect 模式）
* 重构 LocalEngine 构造路径为 builder pattern
* gRPC server streaming 的 Python async generator bridge
* gRPC conversions 已确认完整，无需修改
