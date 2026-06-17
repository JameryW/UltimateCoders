# EngineApi Extension: batch write, list repos, search stream

## Goal

实现 EngineApi trait 的 3 个 TODO(future) 方法，补全引擎 API 能力。

## Requirements

### 1. batch_write_memory
- `async fn batch_write_memory(&self, requests: Vec<MemoryWriteRequest>) -> Result<Vec<MemoryEntry>, EngineError>`
- LocalEngine impl: 遍历 requests，逐个调用 write_memory，收集结果
- 如果任一写入失败，返回第一个错误（原子性非必须，best-effort）
- gRPC: 新增 BatchWriteMemory RPC，proto 定义

### 2. list_repos
- `async fn list_repos(&self) -> Result<Vec<RepoIndexState>, EngineError>`
- LocalEngine impl: 遍历内存索引，返回所有 repo 状态
- gRPC: 新增 ListRepos RPC

### 3. search_stream
- `async fn search_stream(&self, query: SearchQuery) -> Result<Pin<Box<dyn Stream<Item = SearchResult> + Send>>, EngineError>`
- LocalEngine impl: 返回单元素 stream（当前搜索引擎非真正流式）
- gRPC: 新增 SearchStream server-streaming RPC
- 注意：async trait 方法返回 Stream 需要用 `impl Stream` 或 Box 化

## Acceptance Criteria

* [ ] batch_write_memory: 写入多条 memory，返回所有 entry
* [ ] list_repos: 返回已索引仓库列表
* [ ] search_stream: 返回 stream，consumer 逐条接收 SearchResult
* [ ] LocalEngine 实现全部 3 个方法
* [ ] gRPC proto + client/server 实现全部 3 个 RPC
* [ ] PyO3 bridge 暴露全部 3 个方法
* [ ] 测试覆盖

## Out of Scope

* 真正的流式搜索引擎（当前返回单元素 stream）
* 批量写入的原子性保证
