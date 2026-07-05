#!/usr/bin/env bash
# Shared dashboard auto-start — sourced by run-omp.sh and run-cluster.sh.
# Starts FastAPI backend (:8080) + Vite dev (:5173) for the web dashboard.
#
# Expects: SCRIPT_DIR, LOG_DIR set by caller. Exports: START_DASHBOARD,
# DASH_PID, VITE_PID, and function start_dashboard.
# Caller parses --no-dashboard to set START_DASHBOARD=false AFTER sourcing
# (the default here is true; the flag parse overrides it).

START_DASHBOARD="${START_DASHBOARD:-true}"
DASH_PID=""
VITE_PID=""

# ── Start dashboard backend (:8080) + Vite dev (:5173) ──────────
# Called before OMP launch. Default on; --no-dashboard disables.
# Best-effort: no NATS → SSE empty, gRPC-Web still works.
start_dashboard() {
    if [ "$START_DASHBOARD" != true ]; then
        return 0
    fi
    # Backend (FastAPI :8080) — needs .venv/bin/python
    if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        echo ">>> warn: .venv/bin/python not found — dashboard backend skipped" >&2
    else
        echo ">>> Starting dashboard backend on :8080..."
        # Export UC_NATS_URL so the dashboard process inherits it.
        # In --docker mode it's already set to the NATS container; otherwise
        # the default points at localhost (best-effort — SSE empty if no NATS).
        UC_NATS_URL="${UC_NATS_URL:-nats://127.0.0.1:4222}" \
          "$SCRIPT_DIR/.venv/bin/python" -m ultimate_coders.dashboard \
          --host 0.0.0.0 --port 8080 >> "$LOG_DIR/dashboard.log" 2>&1 &
        DASH_PID=$!
        echo "    Dashboard PID: $DASH_PID (logs: $LOG_DIR/dashboard.log)"
        echo "    Dashboard: http://localhost:5173 (API: http://localhost:8080/dashboard/)"
    fi
    # Frontend (Vite :5173) — needs bun + node_modules
    if ! command -v bun >/dev/null 2>&1; then
        echo ">>> warn: bun not found — dashboard frontend skipped (backend still on :8080)" >&2
    else
        if [ ! -d "$SCRIPT_DIR/dashboard/node_modules" ]; then
            echo ">>> dashboard/node_modules missing — running bun install..."
            (cd "$SCRIPT_DIR/dashboard" && bun install) \
              || echo ">>> warn: bun install failed — frontend skipped" >&2
        fi
        if [ -d "$SCRIPT_DIR/dashboard/node_modules" ]; then
            echo ">>> Starting dashboard frontend (Vite) on :5173..."
            (cd "$SCRIPT_DIR/dashboard" && bun run dev) >> "$LOG_DIR/dashboard-vite.log" 2>&1 &
            VITE_PID=$!
            echo "    Vite PID: $VITE_PID (logs: $LOG_DIR/dashboard-vite.log)"
        fi
    fi
}
