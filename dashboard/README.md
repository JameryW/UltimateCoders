# UltimateCoders Dashboard

The browser UI has two routes: `/` for the product overview and
`#/dashboard` (also `/dashboard`) for live task, worker, event, scheduler,
search, file, and metrics views. Both use the Rust Gateway through gRPC-Web.
The Dashboard API provides REST and SSE snapshots as a fallback.

The old browser OMP terminal (`#/tui`) and its `/ws/tui` PTY bridge have been
removed. Native OMP remains available separately through `run-omp.sh` and the
`packages/uc-orchestrator` extension.

## Runtime flow

```text
Browser overview `/` → gRPC-Web → Gateway :50051
Operations dashboard `#/dashboard`
  ├─ Task / Worker / Event / Scheduler panels → Gateway DashboardService
  ├─ Search / File / Repository panels → Gateway EngineService
  └─ REST / SSE fallback → Dashboard API :8080
```

## Local commands

```bash
pnpm install
pnpm run dev
pnpm run build
pnpm run lint
pnpm run test
```

Vite serves `http://127.0.0.1:5173/` and proxies gRPC-Web requests to
`127.0.0.1:50051` and Dashboard REST/SSE requests to `127.0.0.1:8080`.