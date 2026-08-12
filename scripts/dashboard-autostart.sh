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
DASH_PYTHON=""
DASHBOARD_PM=""

resolve_dashboard_python() {
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        DASH_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    elif [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
        DASH_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    elif [ -x "$SCRIPT_DIR/.venv/Scripts/python.exe" ]; then
        DASH_PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
    elif [ -x "$SCRIPT_DIR/.venv/Scripts/python" ]; then
        DASH_PYTHON="$SCRIPT_DIR/.venv/Scripts/python"
    elif command -v python3 >/dev/null 2>&1; then
        DASH_PYTHON="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        DASH_PYTHON="$(command -v python)"
    fi
}

resolve_dashboard_package_manager() {
    if command -v bun >/dev/null 2>&1; then
        DASHBOARD_PM="bun"
    elif command -v pnpm >/dev/null 2>&1; then
        DASHBOARD_PM="pnpm"
    elif command -v npm >/dev/null 2>&1; then
        DASHBOARD_PM="npm"
    fi
}

# ── Start dashboard backend (:8080) + Vite dev (:5173) ──────────
# Called before OMP launch. Default on; --no-dashboard disables.
# Best-effort: no NATS → SSE empty, gRPC-Web still works.
start_dashboard() {
    if [ "$START_DASHBOARD" != true ]; then
        return 0
    fi
    # Backend (FastAPI :8080) — prefer the project venv, but also support a
    # system Python installation. The old fixed .venv check skipped the API
    # even when `python -m pip install -e .` had already been used.
    resolve_dashboard_python
    DASH_PYTHONPATH="$SCRIPT_DIR/python"
    if [ -n "${PYTHONPATH:-}" ]; then
        DASH_PYTHONPATH="$DASH_PYTHONPATH:$PYTHONPATH"
    fi
    if [ -z "$DASH_PYTHON" ]; then
        echo ">>> warn: no Python interpreter found — dashboard backend skipped" >&2
    elif ! PYTHONPATH="$DASH_PYTHONPATH" "$DASH_PYTHON" -c 'import fastapi, uvicorn, ultimate_coders.dashboard' >/dev/null 2>&1; then
        echo ">>> warn: dashboard Python dependencies are missing — install with: python -m pip install -e ." >&2
    else
        echo ">>> Starting dashboard backend on :8080..."
        # Export UC_NATS_URL so the dashboard process inherits it.
        # In --docker mode it's already set to the NATS container; otherwise
        # the default points at localhost (best-effort — SSE empty if no NATS).
        PYTHONPATH="$DASH_PYTHONPATH" UC_NATS_URL="${UC_NATS_URL:-nats://127.0.0.1:4222}" \
          "$DASH_PYTHON" -m ultimate_coders.dashboard \
          --host 0.0.0.0 --port 8080 >> "$LOG_DIR/dashboard.log" 2>&1 &
        DASH_PID=$!
        echo "    Dashboard PID: $DASH_PID (logs: $LOG_DIR/dashboard.log)"
        echo "    Dashboard: http://localhost:5173 (API: http://localhost:8080/dashboard/)"
    fi
    # Frontend (Vite :5173) — use Bun when available, then pnpm/npm for local
    # installs that already have Node.js but not Bun.
    resolve_dashboard_package_manager
    if [ -z "$DASHBOARD_PM" ]; then
        echo ">>> warn: bun/pnpm/npm not found — dashboard frontend skipped (backend still on :8080)" >&2
    else
        if [ ! -d "$SCRIPT_DIR/dashboard/node_modules" ]; then
            echo ">>> dashboard/node_modules missing — installing frontend dependencies with $DASHBOARD_PM..."
            case "$DASHBOARD_PM" in
              bun) (cd "$SCRIPT_DIR/dashboard" && bun install) ;;
              pnpm) (cd "$SCRIPT_DIR/dashboard" && pnpm install --frozen-lockfile) ;;
              npm) (cd "$SCRIPT_DIR/dashboard" && npm install --no-package-lock) ;;
            esac \
              || echo ">>> warn: frontend dependency install failed — frontend skipped" >&2
        fi
        if [ -d "$SCRIPT_DIR/dashboard/node_modules" ]; then
            echo ">>> Starting dashboard frontend (Vite) on :5173..."
            case "$DASHBOARD_PM" in
              bun) (cd "$SCRIPT_DIR/dashboard" && bun run dev) ;;
              pnpm) (cd "$SCRIPT_DIR/dashboard" && pnpm run dev) ;;
              npm) (cd "$SCRIPT_DIR/dashboard" && npm run dev) ;;
            esac >> "$LOG_DIR/dashboard-vite.log" 2>&1 &
            VITE_PID=$!
            echo "    Vite PID: $VITE_PID (logs: $LOG_DIR/dashboard-vite.log)"
        fi
    fi
}
