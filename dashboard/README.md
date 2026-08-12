# UltimateCoders Dashboard

This package is the browser dashboard for UltimateCoders. The root route `/`
is the product overview: it explains the orchestration, worker, search/memory,
event, and deployment layers, shows the live Gateway connection state, and
provides interactive OMP and command capability walkthroughs. The real OMP
terminal is available at `#/tui` and keeps the command bar connected to the
Gateway TaskService over gRPC-Web. The existing operations dashboard remains
available at `#/dashboard`, so the product overview is an entry layer rather
than a replacement for task, worker, event, scheduler, search, file, and
metrics monitoring.

## Runtime flow

```text
Dashboard homepage `/`
  ├─ Runtime Surface → gRPC-Web → Gateway :50051
  ├─ Product Map → Command / Control / Execution / Knowledge / Event planes
  ├─ OMP ↔ UC Loop → Extension → Gateway → Worker → TaskEvent
  └─ Command Deck → run / status / tasks / workers / search / logs

Real terminal `#/tui`
  ├─ gRPC-Web → Gateway :50051
  └─ WebSocket /ws/tui → FastAPI :8080 → ConPTY → run-omp.sh → OMP

Operations dashboard `#/dashboard`
  ├─ Task / Worker / Event / Scheduler panels → DashboardService
  ├─ Search / File / Repository panels → Engine and index services
  └─ Metrics / interaction log → live gRPC-Web event streams
```

The FastAPI process is a bridge for the OMP PTY; it is not a second web UI.
The OMP PTY is intentionally single-session. If another dashboard tab is
attached, `#/tui` shows `session attached elsewhere`; use `Take over` to move
the same persistent PTY to the current tab without restarting OMP.

## Local commands

```bash
pnpm install
pnpm run dev
pnpm run build
pnpm run lint
```

The default Vite URL is `http://127.0.0.1:5173/`. To reproduce the current
local handoff port, run `pnpm run dev -- --host 127.0.0.1 --port 4176` and use
`http://127.0.0.1:4176/`; the live terminal remains at `#/tui`.

The Vite development server proxies gRPC-Web requests to `127.0.0.1:50051`
and `/ws/tui` to `127.0.0.1:8080`.
