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
      echo "  (default)  Start OMP with UC Orchestrator extension"
      echo "  --server   also start gRPC server in background"
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

# ── Start gRPC server if requested ────────────────────────────
if [ "$START_DASH" = true ]; then
    if ! lsof -i :50051 >/dev/null 2>&1; then
        echo ">>> Starting gRPC server..."
        cd "$PROJECT_ROOT"
        PATH="$PROJECT_ROOT/.venv/bin:$PATH" RUST_LOG="${RUST_LOG:-info}" \
            cargo run -p uc-grpc-server &
        SERVER_PID=$!
        echo "    Server PID: $SERVER_PID"
        for i in $(seq 1 20); do
            if lsof -i :50051 >/dev/null 2>&1; then
                echo "    Server ready on :50051"
                break
            fi
            sleep 0.5
        done
    fi
fi

# Start OMP with UC Orchestrator extension
echo ">>> Starting OMP with UC Orchestrator..."
cd "$PROJECT_ROOT"
exec bash run-omp.sh
