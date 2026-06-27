#!/usr/bin/env bash
set -euo pipefail

# Run OMP with UC Orchestrator extension loaded
# This is the primary way to interact with UltimateCoders.
#
# Usage: ./run-omp.sh [options]
#   --no-server   skip gRPC server startup (server starts by default)
#   --docker      use Docker Compose for storage backends (TiKV/Qdrant/PG/NATS)
#   --build       ensure Python package is built (maturin develop)
#   --help        show this help
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
USE_DOCKER=false
for arg in "$@"; do
  case "$arg" in
    --no-server) START_SERVER=false ;;
    --docker)    USE_DOCKER=true ;;
    --build)     DO_BUILD=true ;;
    --help|-h)
      echo "Usage: $0 [--no-server] [--docker] [--build]"
      echo ""
      echo "  --no-server   skip gRPC server startup (server starts by default)"
      echo "  --docker      use Docker Compose for storage backends (TiKV/Qdrant/PG/NATS)"
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

# ── Start Docker storage backends ──────────────────────────────
if [ "$USE_DOCKER" = true ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo ">>> Error: docker not found — install Docker first" >&2
        exit 1
    fi
    # ponytail: free ports that Docker containers will bind to
    for port in 4222 6333 6334 2379 5432; do
        if lsof -i :$port >/dev/null 2>&1; then
            pid=$(lsof -ti :$port 2>/dev/null || true)
            if [ -n "$pid" ]; then
                echo ">>> Port :$port in use (PID $pid) — stopping"
                kill $pid 2>/dev/null || true
            fi
        fi
    done
    sleep 1
    echo ">>> Starting Docker storage backends (TiKV, Qdrant, PG, NATS)..."
    cd "$SCRIPT_DIR/docker" && docker compose up -d pd tikv qdrant postgres nats
    echo "    Waiting for backends to be healthy..."
    for i in $(seq 1 60); do
        unhealthy=$(docker compose ps --format json 2>/dev/null \
            | grep -c '"Health":"unhealthy"' || true)
        starting=$(docker compose ps --format json 2>/dev/null \
            | grep -c '"Health":"starting"' || true)
        if [ "$unhealthy" -eq 0 ] && [ "$starting" -eq 0 ]; then
            break
        fi
        sleep 2
    done
    # ponytail: ensure the database exists (stale volumes may lack it)
    docker exec docker-postgres-1 psql -U ultimate_coders -d postgres \
        -c "SELECT 1 FROM pg_database WHERE datname='ultimate_coders'" 2>/dev/null \
        | grep -q 1 || {
        echo "    Creating PostgreSQL database 'ultimate_coders'..."
        docker exec docker-postgres-1 psql -U ultimate_coders -d postgres \
            -c "CREATE DATABASE ultimate_coders;" 2>/dev/null
    }
    export UC_TIKV_PD_ENDPOINTS="${UC_TIKV_PD_ENDPOINTS:-127.0.0.1:2379}"
    export UC_QDRANT_URL="${UC_QDRANT_URL:-http://127.0.0.1:6334}"
    export UC_PG_URL="${UC_PG_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}"
fi

# ── Start gRPC server (default) ───────────────────────────────
SERVER_PID=""
LOG_DIR="$SCRIPT_DIR/.logs"
mkdir -p "$LOG_DIR"

# Reap zombie child processes (Bun/OMP doesn't wait() on children)
# ponytail: SIGCHLD handler prevents zombie accumulation when gRPC server dies
reap_children() {
    while wait -n 2>/dev/null; do :; done
}
trap reap_children CHLD

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ">>> Stopping gRPC server (PID $SERVER_PID)..."
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    # Reap any remaining zombies
    reap_children
    if [ "$USE_DOCKER" = true ]; then
        echo ">>> Stopping Docker storage backends..."
        cd "$SCRIPT_DIR/docker" && docker compose down 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Health monitor: restart gRPC server if it dies
health_monitor() {
    while true; do
        sleep 10
        if [ -n "$SERVER_PID" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo ">>> gRPC server died (PID $SERVER_PID), restarting..."
            wait "$SERVER_PID" 2>/dev/null || true
            echo ">>> Logs: $LOG_DIR/grpc-server.log"
            echo ">>> Last 10 lines:"
            tail -10 "$LOG_DIR/grpc-server.log" 2>/dev/null
            cd "$SCRIPT_DIR"
            PATH="$SCRIPT_DIR/.venv/bin:$PATH" RUST_LOG="${RUST_LOG:-info}" \
                cargo run -p uc-grpc-server >> "$LOG_DIR/grpc-server.log" 2>&1 &
            SERVER_PID=$!
            echo ">>> Restarted gRPC server (PID $SERVER_PID)"
            # Wait for port to be ready
            for i in $(seq 1 20); do
                if lsof -i :50051 >/dev/null 2>&1; then
                    echo ">>> Server ready on :50051"
                    break
                fi
                sleep 0.5
            done
        fi
    done
}

if [ "$START_SERVER" = true ]; then
    if ! lsof -i :50051 >/dev/null 2>&1; then
        echo ">>> Starting gRPC server..."
        cd "$SCRIPT_DIR"
        # ponytail: build env vars — Docker mode adds storage + CORS + NATS
        GRPC_ENV=(
            PATH="$SCRIPT_DIR/.venv/bin:$PATH"
            RUST_LOG="${RUST_LOG:-info}"
            UC_NATS_URL="${UC_NATS_URL:-nats://127.0.0.1:4222}"
            UC_CORS_MODE="${UC_CORS_MODE:-dev}"
        )
        if [ "$USE_DOCKER" = true ]; then
            GRPC_ENV+=(
                UC_TIKV_PD_ENDPOINTS="${UC_TIKV_PD_ENDPOINTS:-127.0.0.1:2379}"
                UC_QDRANT_URL="${UC_QDRANT_URL:-http://127.0.0.1:6334}"
                UC_PG_URL="${UC_PG_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}"
                UC_DATABASE_URL="${UC_DATABASE_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}"
                UC_TASK_BACKEND="${UC_TASK_BACKEND:-postgres}"
            )
        fi
        env "${GRPC_ENV[@]}" cargo run -p uc-grpc-server >> "$LOG_DIR/grpc-server.log" 2>&1 &
        SERVER_PID=$!
        echo "    Server PID: $SERVER_PID"
        echo "    Logs: $LOG_DIR/grpc-server.log"
        for i in $(seq 1 20); do
            if lsof -i :50051 >/dev/null 2>&1; then
                echo "    Server ready on :50051"
                break
            fi
            sleep 0.5
        done
        # Start health monitor in background
        health_monitor &
    else
        echo ">>> gRPC server already running on :50051"
    fi
fi

# ── Start OMP with UC Orchestrator ──────────────────────────
cd "$SCRIPT_DIR/vendor/oh-my-pi"
exec bun packages/coding-agent/src/cli.ts \
  --extension ../../packages/uc-orchestrator \
  "$@"
