#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Parse flags ───────────────────────────────────────────────
START_DASH=false
for arg in "$@"; do
  case "$arg" in
    --server) START_DASH=true ;;
    --help|-h)
      echo "Usage: $0 [--server]"
      echo ""
      echo "  (default)  TUI only (auto-starts gRPC server)"
      echo "  --server   also start web dashboard in background"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

# Auto-detect .venv Python for the gRPC worker
if [ -x "$PROJECT_ROOT/.venv/bin/python3" ]; then
    export UC_WORKER_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
fi

# Ensure Python package is built (maturin develop)
if [ -x "$PROJECT_ROOT/.venv/bin/python3" ]; then
    "$PROJECT_ROOT/.venv/bin/python3" -c "import ultimate_coders" 2>/dev/null || {
        echo ">>> Building ultimate_coders Python package..."
        cd "$PROJECT_ROOT" && maturin develop --manifest-path crates/uc-python/Cargo.toml
    }
fi

# Ensure gRPC server is running
if ! lsof -i :50051 >/dev/null 2>&1; then
    echo ">>> Starting gRPC server..."
    cd "$PROJECT_ROOT"
    PATH="$PROJECT_ROOT/.venv/bin:$PATH" RUST_LOG="${RUST_LOG:-info}" \
        cargo run -p uc-grpc-server &
    SERVER_PID=$!
    echo "    Server PID: $SERVER_PID"
    # Wait for server to be ready
    for i in $(seq 1 20); do
        if lsof -i :50051 >/dev/null 2>&1; then
            echo "    Server ready on :50051"
            break
        fi
        sleep 0.5
    done
fi

# ── Start dashboard if requested ──────────────────────────────
DASH_PID=""
if [ "$START_DASH" = true ]; then
    if [ ! -d "$PROJECT_ROOT/dashboard/node_modules" ]; then
        echo ">>> Installing Dashboard dependencies..."
        cd "$PROJECT_ROOT/dashboard" && npm install
    fi
    echo ">>> Starting dashboard..."
    (cd "$PROJECT_ROOT/dashboard" && npx vite --host) &
    DASH_PID=$!
    for i in $(seq 1 20); do
        if curl -s http://localhost:5173 >/dev/null 2>&1; then break; fi
        sleep 0.5
    done
    echo "    Dashboard: http://localhost:5173"
fi

cleanup() {
    if [ -n "$DASH_PID" ]; then
        echo ">>> Stopping dashboard (PID $DASH_PID)..."
        kill "$DASH_PID" 2>/dev/null || true
        wait "$DASH_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Start TUI
echo ">>> Starting TUI..."
cd "$PROJECT_ROOT/tui"
npx tsx src/index.tsx
