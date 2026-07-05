# Dashboard auto-start with TUI (run-omp.sh integration)

## Goal

运行 TUI（./run-omp.sh）时 dashboard 一起启动，无需手动单独跑。dashboard 前端 Vite dev :5173 + 后端 FastAPI :8080，访问 http://localhost:5173 看 worker 实时进度/coding agent/gateway 状态。

## What I already know

- dashboard 前端：`cd dashboard && bun run dev` → Vite :5173（vite.config.ts proxy /dashboard/api→:8080, gRPC-Web→:50051）
- dashboard 后端：`DashboardApp(orchestrator).start(host, port=8080)`（python/ultimate_coders/dashboard/app.py:1530），uvicorn 后台线程
- **无独立 CLI 入口** — `python -m ultimate_coders.cli start --dashboard`（compose command）指向不存在的 `ultimate_coders.cli` 模块（过时）。DashboardApp 需 Python 实例化。
- DashboardApp `orchestrator` 参数可 None（init 防护 `if orchestrator else None`）。endpoints 有 None guard 返回 503。SSE /stream 从 NATS uc.task.event（不依赖 orchestrator）。前端经 gRPC-Web :50051 取数据（Rust gRPC server，非 orchestrator）。
- run-omp.sh 现有：gRPC server（:50051，health_monitor + cleanup trap）+ OMP TUI（exec）。无 dashboard。
- run-omp.sh cleanup trap（line 174-181）杀 SERVER_PID + docker down。需加 dashboard 进程清理。
- .venv/bin/python 存在；ultimate_coders 包需 maturin develop（--build flag）

## 方案

1. 写 `python/ultimate_coders/dashboard/__main__.py` — `python -m ultimate_coders.dashboard` 启 DashboardApp(orchestrator=None, nats_client=连 NATS)。无 orchestrator：REST submit/pause 返回 503，但 SSE + gRPC-Web 路径正常。
2. run-omp.sh 加 `--dashboard` flag（默认 on？或 opt-in）：启 Vite dev :5173（后台）+ Python dashboard :8080（后台）。两 PID 加入 cleanup trap。
3. NATS 连接：dashboard 后端连 UC_NATS_URL（默认 nats://127.0.0.1:4222）。run-omp.sh --docker 模式 NATS 在容器；非 docker 模式需 NATS 跑着（run-cluster.sh 启 NATS，run-omp.sh 不启）。

## Decision (ADR-lite)

**Context**: 用户要运行 TUI 时 dashboard 一起启动，无需手动跑。dashboard 前端 Vite :5173 + 后端 FastAPI :8080。
**Decision**:
1. 写 `python/ultimate_coders/dashboard/__main__.py` — `python -m ultimate_coders.dashboard` 启 DashboardApp(orchestrator=None) + 连 NATS（UC_NATS_URL，连不上则 SSE 空流，gRPC-Web 路径仍工作）
2. run-omp.sh **默认 on** 启 dashboard（--no-dashboard 禁用）：
   - 启 Python 后端 :8080（`.venv/bin/python -m ultimate_coders.dashboard`，后台）
   - 启 Vite dev :5173（`cd dashboard && bun run dev`，后台）
   - dashboard/node_modules 缺失 → 自动 `bun install` 再启
   - 两 PID 加入 cleanup trap
3. 非 docker 模式（无 NATS）也启 dashboard 后端 — SSE 空流，gRPC-Web :50051 仍供数据
**Consequences**: run-omp.sh 多管两进程（dashboard 后端 + Vite）。bun 必须在 PATH（自动 install 需要）。无 NATS 时 dashboard 功能受限（无实时事件流）但可访问。

## Requirements

- `python -m ultimate_coders.dashboard` 启动后端 :8080（DashboardApp orchestrator=None，连 NATS best-effort）
- run-omp.sh 默认启 dashboard 前端 :5173 + 后端 :8080
- --no-dashboard flag 禁用
- dashboard/node_modules 缺失自动 `bun install`
- cleanup trap 杀 dashboard 后端 + Vite 进程
- 访问 http://localhost:5173 可用

## Acceptance Criteria

- [ ] `python -m ultimate_coders.dashboard` 启 :8080，SSE 连 NATS（连不上不崩）
- [ ] ./run-omp.sh 启 TUI + dashboard（:5173 + :8080），访问 :5173 见 dashboard
- [ ] dashboard/node_modules 缺失时自动 bun install 后启 Vite
- [ ] Ctrl+C 退出清 dashboard 两进程
- [ ] --no-dashboard 不启 dashboard
- [ ] 非 docker 模式（无 NATS）dashboard 仍启，gRPC-Web 路径工作

## Implementation Plan (small PRs)

- PR1: `python/ultimate_coders/dashboard/__main__.py` — 启动入口（DashboardApp(None) + NATS best-effort）
- PR2: run-omp.sh 集成 — --no-dashboard flag + 启 :8080 + :5173（自动 bun install）+ cleanup trap

## Out of Scope

- dashboard 嵌入 OMP TUI 进程内（独立进程方案足够）
- 生产 build 静态文件 serve（dev 模式即可）
- run-cluster.sh dashboard 集成（先 run-omp.sh，cluster 后续）

## Technical Notes

- dashboard/app.py:1530 start(), :84 __init__(orchestrator=None 可)
- vite.config.ts proxy :5173→:8080/:50051
- run-omp.sh:174 cleanup trap, :50051 gRPC, --docker NATS
- .venv/bin/python, maturin develop via --build
