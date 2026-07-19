# PRD: Fix Nested block_in_place Panic in OrchestratorDispatcher

## 背景

Rust gRPC server 审计 finding #3。`OrchestratorDispatcher::dispatch` (dispatcher.rs:78) 用 `block_in_place(|| Handle::current().block_on(...))` 同步等 NATS request-reply。成功后调 `_process_decomposition_reply` (L92)，其内部 L190 **再 block_in_place** 发每个 subtask 的 publish。

## Bug

nested `block_in_place` → **必 panic** "cannot block_in_place within a block_in_place"。触发：任何含 subtasks 的成功 decomposition reply（即 scheduled task 正常派发路径）。

外层 block_in_place 已把当前线程转阻塞模式，内层不能再 block_in_place。但 `block_on` 在 block_in_place 内合法。

## 改

`_process_decomposition_reply` L190-193：去掉 `block_in_place`，保留 `Handle::current().block_on(...)`。publish 是 fire-and-forget（`let _ =`），block_on 等其完成即可。

## 验收

- cargo check/test -p uc-engine（messaging feature）。
- 逻辑推理：外层 block_in_place 内调 sync fn，fn 内 block_on（非 block_in_place）合法，不 panic。
- feature branch + PR。
- PR 后查 CI（ci-rust）。

## 不做

- 不改 dispatch() 的外层 block_in_place（#2，非 nested，需另评 runtime 要求）。
- 不改 #1 server.rs expect（另立）。
- 不重构为全 async（scope 外）。
