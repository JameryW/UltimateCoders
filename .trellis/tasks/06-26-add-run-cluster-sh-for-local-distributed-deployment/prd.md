# Add run-cluster.sh for Local Distributed Deployment

## Goal

一键启动本地分布式集群——NATS + gRPC server + N 个 NATS Worker + OMP，让开发者无需手动拼命令。

## Requirements

* `run-cluster.sh` 脚本，支持以下模式：
  - 默认：NATS + gRPC server + N worker + OMP（全链路）
  - `--workers N`：启动 N 个 worker（默认 2）
  - `--no-omp`：只启动后端（NATS + gRPC server + workers），不启动 OMP
  - `--docker`：用 Docker Compose 启动存储后端（TiKV/Qdrant/PG/NATS），再本地跑 gRPC + workers
  - `--build`：确保 `ultimate_coders` 已构建
  - `--stop`：停止所有已启动的进程
* 自动检测依赖（nats-server, python, cargo, bun）
* 自动检测端口占用，避免冲突
* Ctrl+C 正确清理所有子进程
* 彩色输出，显示各组件状态

## Acceptance Criteria

* [ ] `./run-cluster.sh` 一键启动完整集群
* [ ] `./run-cluster.sh --workers 4` 启动 4 个 worker
* [ ] `./run-cluster.sh --no-omp` 只启动后端
* [ ] `./run-cluster.sh --docker` 用 Docker 提供存储后端
* [ ] `./run-cluster.sh --stop` 停止所有进程
* [ ] Ctrl+C 清理所有子进程
* [ ] `uc_task submit` 在集群模式下正常工作

## Out of Scope

* Docker 化 worker 本身
* 远程/云端部署
* TLS/认证配置

## Technical Notes

* NATS Worker: `python -m ultimate_coders.nats_worker`，需 `nats-py` 库
* gRPC Server: `cargo run -p uc-grpc-server`，连 NATS
* NATS 默认端口 4222，gRPC 50051
* docker-compose.yml 在 `docker/` 目录
* Worker 依赖 `ultimate_coders` Python package（maturin develop）
