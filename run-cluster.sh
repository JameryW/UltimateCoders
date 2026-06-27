#!/usr/bin/env bash
set -euo pipefail

# Local distributed cluster startup for UltimateCoders.
#
# Starts: NATS server → gRPC server → N NATS Workers → OMP (optional)
#
# Usage: ./run-cluster.sh [options]
#   --workers N     start N workers (default: 2)
#   --no-omp        skip OMP startup (just backend + workers)
#   --docker        use Docker Compose for storage backends (TiKV/Qdrant/PG/NATS)
#   --build         ensure ultimate_coders Python package is built
#   --stop          stop all previously started processes
#   --help          show this help

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDS_FILE="$SCRIPT_DIR/.cluster-pids"

# ── Colors ─────────────────────────────────────────────────────
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
DIM="\033[2m"
RESET="\033[0m"

log()  { echo -e "${GREEN}>>>${RESET} $1"; }
warn() { echo -e "${YELLOW}>>>${RESET} $1"; }
err()  { echo -e "${RED}>>>${RESET} $1"; }
info() { echo -e "${CYAN}   ${RESET} $1"; }

# ── Defaults ───────────────────────────────────────────────────
NUM_WORKERS=2
NO_OMP=false
USE_DOCKER=false
DO_BUILD=false
DO_STOP=false

for arg in "$@"; do
  case "$arg" in
    --workers)  : ;; # value parsed below
    --workers=*) NUM_WORKERS="${arg#*=}" ;;
    --no-omp)   NO_OMP=true ;;
    --docker)   USE_DOCKER=true ;;
    --build)    DO_BUILD=true ;;
    --stop)     DO_STOP=true ;;
    --help|-h)
      echo "Usage: $0 [options]"
      echo ""
      echo "  --workers N     start N workers (default: 2)"
      echo "  --no-omp        skip OMP startup (just backend + workers)"
      echo "  --docker        use Docker Compose for storage backends"
      echo "  --build         ensure ultimate_coders Python package is built"
      echo "  --stop          stop all previously started processes"
      echo "  --help          show this help"
      exit 0
      ;;
    *) ;;
  esac
done

# ponytail: --workers N (separate arg) — iterate with index to peek next arg
ARGS=("$@")
for (( i=0; i<${#ARGS[@]}; i++ )); do
  if [[ "${ARGS[i]}" == --workers ]] && [[ "${ARGS[i+1]}" =~ ^[0-9]+$ ]]; then
    NUM_WORKERS="${ARGS[i+1]}"
  fi
done

# ── Load environment ───────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Detect Python ──────────────────────────────────────────────
PYTHON_BIN=""
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
    export UC_WORKER_PYTHON="$PYTHON_BIN"
else
    PYTHON_BIN="python3"
fi

# ── Stop mode ──────────────────────────────────────────────────
if [ "$DO_STOP" = true ]; then
    if [ -f "$PIDS_FILE" ]; then
        log "Stopping cluster processes..."
        while read -r pid name; do
            if kill -0 "$pid" 2>/dev/null; then
                info "Killing $name (PID $pid)"
                kill "$pid" 2>/dev/null || true
            else
                info "$name (PID $pid) already gone"
            fi
        done < "$PIDS_FILE"
        wait 2>/dev/null || true
        rm -f "$PIDS_FILE"

        if [ "$USE_DOCKER" = true ]; then
            log "Stopping Docker Compose..."
            cd "$SCRIPT_DIR/docker" && docker compose down 2>/dev/null || true
        fi

        log "Cluster stopped."
    else
        warn "No $PIDS_FILE found — nothing to stop."
    fi
    exit 0
fi

# ── Build Python package ──────────────────────────────────────
if [ "$DO_BUILD" = true ] && [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    "$PYTHON_BIN" -c "import ultimate_coders" 2>/dev/null || {
        log "Building ultimate_coders Python package..."
        cd "$SCRIPT_DIR" && maturin develop --manifest-path crates/uc-python/Cargo.toml
    }
fi

# ── Prereq checks ──────────────────────────────────────────────
check_port() {
    if lsof -i :"$1" >/dev/null 2>&1; then
        warn "Port :$1 already in use — component may already be running"
        return 1
    fi
    return 0
}

check_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "$1 not found — install it first"
        return 1
    fi
    return 0
}

# ── Cleanup trap ───────────────────────────────────────────────
PIDS=()
cleanup() {
    log "Shutting down cluster..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    rm -f "$PIDS_FILE"
    if [ "$USE_DOCKER" = true ]; then
        cd "$SCRIPT_DIR/docker" && docker compose down 2>/dev/null || true
    fi
    log "Done."
}
trap cleanup EXIT INT TERM

save_pid() {
    PIDS+=("$1")
    echo "$1 $2" >> "$PIDS_FILE"
}

# ── Start Docker storage backends ──────────────────────────────
if [ "$USE_DOCKER" = true ]; then
    check_cmd docker || exit 1
    # ponytail: free ports that Docker containers will bind to
    for port in 4222 6333 6334 2379 5432; do
        if lsof -i :$port >/dev/null 2>&1; then
            pid=$(lsof -ti :$port 2>/dev/null || true)
            if [ -n "$pid" ]; then
                warn "Port :$port in use (PID $pid) — stopping"
                kill $pid 2>/dev/null || true
            fi
        fi
    done
    sleep 1
    log "Starting Docker storage backends (TiKV, Qdrant, PG, NATS)..."
    cd "$SCRIPT_DIR/docker" && docker compose up -d pd tikv qdrant postgres nats
    info "Waiting for backends to be healthy..."
    for i in $(seq 1 60); do
        # ponytail: check each service individually — some take longer
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
        info "Creating PostgreSQL database 'ultimate_coders'..."
        docker exec docker-postgres-1 psql -U ultimate_coders -d postgres \
            -c "CREATE DATABASE ultimate_coders;" 2>/dev/null
    }
    NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
    # ponytail: Docker mode → storage env vars point to localhost ports
    export UC_TIKV_PD_ENDPOINTS="${UC_TIKV_PD_ENDPOINTS:-127.0.0.1:2379}"
    export UC_QDRANT_URL="${UC_QDRANT_URL:-http://127.0.0.1:6334}"
    export UC_PG_URL="${UC_PG_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}"
fi

# ── Start NATS server (if not using Docker) ────────────────────
if [ "$USE_DOCKER" = false ]; then
    check_cmd nats-server || { warn "nats-server not found — install: brew install nats-server"; exit 1; }
    if check_port 4222; then
        log "Starting NATS server..."
        nats-server -js -sd "$SCRIPT_DIR/.nats-data" &
        NATS_PID=$!
        save_pid "$NATS_PID" "nats-server"
        info "NATS PID: $NATS_PID"
        for i in $(seq 1 10); do
            if check_port 4222; then sleep 0.5; else break; fi
        done
        log "NATS ready on :4222"
    else
        log "NATS server already running on :4222"
    fi
fi
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

# ── Start gRPC server ──────────────────────────────────────────
if check_port 50051; then
    log "Starting gRPC server..."
    cd "$SCRIPT_DIR"
    GRPC_ENV=(
        PATH="$SCRIPT_DIR/.venv/bin:$PATH"
        RUST_LOG="${RUST_LOG:-info}"
        UC_NATS_URL="$NATS_URL"
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
    # Build first, then exec the binary directly so PID is correct
    log "Building gRPC server..."
    env "${GRPC_ENV[@]}" cargo build -p uc-grpc-server 2>&1 | tail -1
    env "${GRPC_ENV[@]}" nohup "$SCRIPT_DIR/target/debug/uc-grpc-server" > /tmp/uc-grpc-server.log 2>&1 &
    GRPC_PID=$!
    disown "$GRPC_PID"
    save_pid "$GRPC_PID" "grpc-server"
    info "gRPC PID: $GRPC_PID"
    # Wait for port to become active
    for i in $(seq 1 30); do
        if ! lsof -i :50051 >/dev/null 2>&1; then sleep 1; else break; fi
    done
    if lsof -i :50051 >/dev/null 2>&1; then
        log "gRPC server ready on :50051"
    else
        err "gRPC server failed to start — check /tmp/uc-grpc-server.log"
        exit 1
    fi
else
    log "gRPC server already running on :50051"
fi

# ── Start NATS Workers ─────────────────────────────────────────
# ponytail: check nats-py is installed
"$PYTHON_BIN" -c "import nats" 2>/dev/null || {
    err "nats-py not installed — run: pip install nats-py"
    exit 1
}

log "Starting $NUM_WORKERS NATS workers..."
WORKER_OK=0
for i in $(seq 1 "$NUM_WORKERS"); do
    UC_WORKER_ID="worker-$i" \
    UC_NATS_URL="$NATS_URL" \
    "$PYTHON_BIN" -m ultimate_coders.nats_worker &
    W_PID=$!
    save_pid "$W_PID" "worker-$i"
    info "Worker $i PID: $W_PID"
    # ponytail: check worker didn't immediately die (import error, etc)
    sleep 0.5
    if kill -0 "$W_PID" 2>/dev/null; then
        WORKER_OK=$((WORKER_OK + 1))
    else
        warn "Worker $i exited immediately — check: $PYTHON_BIN -m ultimate_coders.nats_worker"
    fi
done

if [ "$WORKER_OK" -eq 0 ]; then
    err "All workers failed to start. All workers failed to start. Check NATS connectivity and Python environment."
    exit 1
fi

# ponytail: brief wait for workers to connect
sleep 2

# ── Start OMP ──────────────────────────────────────────────────
if [ "$NO_OMP" = false ]; then
    log "Starting OMP..."
    cd "$SCRIPT_DIR/vendor/oh-my-pi"
    exec bun packages/coding-agent/src/cli.ts \
      --extension ../../packages/uc-orchestrator
else
    log "Cluster running (--no-omp mode). Ctrl+C to stop."
    # Keep script alive so trap can cleanup on Ctrl+C
    while true; do sleep 1; done
fi
