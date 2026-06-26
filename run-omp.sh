#!/usr/bin/env bash
set -euo pipefail

# Run OMP with UC Orchestrator extension loaded
# This is the primary way to interact with UltimateCoders.
#
# Usage: ./run-omp.sh [--no-server] [--build]
#   --no-server   skip gRPC server startup (server starts by default)
#   --build       ensure Python package is built (maturin develop)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load local environment (API keys, base URLs, model overrides)
# ponytail: .env is gitignored, safe for secrets
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Parse flags ───────────────────────────────────────────────
START_SERVER=true
DO_BUILD=false
for arg in "$@"; do
  case "$arg" in
    --no-server) START_SERVER=false ;;
    --build)  DO_BUILD=true ;;
    --help|-h)
      echo "Usage: $0 [--no-server] [--build]"
      echo ""
      echo "  --no-server   skip gRPC server startup (server starts by default)"
      echo "  --build       ensure Python package is built (maturin develop)"
      exit 0
      ;;
    *) ;;
  esac
done

# Auto-detect .venv Python for the gRPC worker
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    export UC_WORKER_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
fi

# ── Build Python package if requested ────────────────────────
if [ "$DO_BUILD" = true ] && [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    "$SCRIPT_DIR/.venv/bin/python3" -c "import ultimate_coders" 2>/dev/null || {
        echo ">>> Building ultimate_coders Python package..."
        cd "$SCRIPT_DIR" && maturin develop --manifest-path crates/uc-python/Cargo.toml
    }
fi

# ── Start gRPC server (default) ───────────────────────────────
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        echo ">>> Stopping gRPC server (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

if [ "$START_SERVER" = true ]; then
    if ! lsof -i :50051 >/dev/null 2>&1; then
        echo ">>> Starting gRPC server..."
        cd "$SCRIPT_DIR"
        PATH="$SCRIPT_DIR/.venv/bin:$PATH" RUST_LOG="${RUST_LOG:-info}" \
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
    else
        echo ">>> gRPC server already running on :50051"
    fi
fi

# ── Start OMP with UC Orchestrator ──────────────────────────
cd "$SCRIPT_DIR/vendor/oh-my-pi"
exec bun packages/coding-agent/src/cli.ts \
  --extension ../../packages/uc-orchestrator \
  "$@"
