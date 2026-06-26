# Default gRPC Server + Real Local Worker Deployment

## Goal

让 `run-omp.sh` 默认启动 gRPC server，并确保 local worker 真实可用——使 OMP 中的 `uc_task` tool 不再因 "gRPC server unavailable" 失败。

## What I already know

* `run-omp.sh` 当前 `--server` 是可选 flag（`START_SERVER=false`），不传则不启动 gRPC server
* `uc_task` tool（`task-bridge.ts`）通过 `GrpcBridge` 走 HTTP→gRPC-Web 调 `localhost:50051`
* gRPC server 启动后，`LocalWorkerBridge` 在首次 `submit_task` 时懒启动 Python worker（`ensure_local_worker`）
* Python worker 是 `ultimate_coders.local_worker` 模块，通过 stdin/stdout JSON-RPC 通信
* Worker 需要 `ultimate_coders` Python package 已构建（maturin develop）
* `UC_WORKER_PYTHON` env var 控制 Python 路径，默认 fallback chain: `.venv/bin/python3` → `python3`

## Assumptions (temporary)

* 用户运行 `run-omp.sh` 时，`.venv` 已存在且 `ultimate_coders` 已构建
* 不需要 NATS 即可让 local worker 正常工作（当前代码已支持无 NATS 模式）

## Open Questions

(none remaining)

## Requirements

* `run-omp.sh` 默认启动 gRPC server（不再需要 `--server` flag）
* gRPC server 启动后验证 local worker 可以正常 spawn
* 新增 `--no-server` flag 用于显式跳过 gRPC server 启动
* 脚本退出时正确清理 gRPC server 进程
* `uc_task` tool 区分三种失败场景并返回不同错误信息：
  - gRPC server 不可达（连接拒绝）→ "gRPC server unavailable — start with ./run-omp.sh"
  - gRPC server 在但 worker spawn 失败 → "Worker failed to start: <reason>"
  - gRPC server 响应但 `success: false` → "Submit rejected: <error from server>"

## Acceptance Criteria (evolving)

* [ ] `./run-omp.sh` 不带任何 flag 即启动 gRPC server + OMP
* [ ] `uc_task submit` 在 gRPC server 运行时不再报 "server unavailable"
* [ ] `uc_task submit` 在 server 不可达时报 "gRPC server unavailable — start with ./run-omp.sh"
* [ ] `uc_task submit` 在 worker spawn 失败时报 "Worker failed to start: <reason>"
* [ ] `uc_task submit` 在 server 拒绝时报 "Submit rejected: <error>"
* [ ] `./run-omp.sh --no-server` 可跳过 gRPC server 启动（旧行为）
* [ ] Ctrl+C 正确清理 gRPC server 子进程
* [ ] Worker 启动失败时 `uc_task` 返回有意义的错误信息

## Definition of Done

* 手动验证 `run-omp.sh` 启动后 `uc_task submit` 正常工作
* 脚本退出不留孤儿进程
* CI 不受影响（CI 不用此脚本）

## Out of Scope (explicit)

* NATS 集成
* 远程 worker 部署
* Docker 化 worker

## Technical Notes

* 关键文件：`run-omp.sh`, `crates/uc-grpc/src/local_worker.rs`, `crates/uc-grpc/src/server.rs`
* `GrpcBridge` 默认 `serverUrl: "http://localhost:50051"`
* `LocalWorkerBridge` 用 `resolve_python_bin()` 找 Python，spawn `python -m ultimate_coders.local_worker`
