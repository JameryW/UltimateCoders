#!/usr/bin/env bash
set -euo pipefail

# Run OMP with UC Orchestrator extension loaded
# This is the primary way to interact with UltimateCoders.
#
# Usage: ./run-omp.sh [options]
#   --no-server   skip gRPC server startup (server starts by default)
#   --docker      use Docker Compose for storage backends (TiKV/Qdrant/PG/NATS)
#   --build       ensure Python package (maturin) + release gRPC binary are built
#   --no-dashboard  skip dashboard startup (dashboard starts by default)
#   --help        show this help
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_BIN="$SCRIPT_DIR/target/release/uc-grpc-server"

# OMP workspace setup (cwd + symlink nested configs) — shared with run-cluster.sh.
# ponytail: sourced so both launchers stay in sync; see scripts/omp-workspace.sh.
# shellcheck source=scripts/omp-workspace.sh
source "$SCRIPT_DIR/scripts/omp-workspace.sh"

# ponytail: run the prebuilt release binary instead of `cargo run` each launch.
# cargo run re-checks/relinks every start (~3-10s even when fresh); the binary
# is ~instant. Build it once via --build (or lazily if missing/stale vs source).
ensure_server_bin() {
    if [ -x "$SERVER_BIN" ] && \
       ! find "$SCRIPT_DIR/Cargo.toml" "$SCRIPT_DIR/Cargo.lock" "$SCRIPT_DIR/crates" \
            -newer "$SERVER_BIN" -print -quit 2>/dev/null | grep -q .; then
        return 0  # binary exists and is newer than all source/manifests/lock
    fi
    echo ">>> Building release gRPC server binary (one-time)..."
    cd "$SCRIPT_DIR" && cargo build --release -p uc-grpc-server
}

# ── Start dashboard backend (:8080) + Vite dev (:5173) ──────────
# Called before both OMP launch sites (standalone + final). Default on;
# --no-dashboard disables. Best-effort: no NATS → SSE empty, gRPC-Web still works.
DASH_PID=""
VITE_PID=""
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
STANDALONE=false
NO_SPAWN=false
START_DASHBOARD=true
OMP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-server)  START_SERVER=false ;;
    --docker)     USE_DOCKER=true ;;
    --standalone) STANDALONE=true ;;
    --build)      DO_BUILD=true ;;
    --no-spawn)   NO_SPAWN=true ;;
    --no-dashboard) START_DASHBOARD=false ;;
    --help|-h)
      echo "Usage: $0 [--no-server] [--docker] [--standalone] [--build] [--no-spawn] [--no-dashboard]"
      echo ""
      echo "  --no-server   skip gRPC server startup (server starts by default)"
      echo "  --docker      use Docker Compose for storage backends (TiKV/Qdrant/PG/NATS)"
      echo "  --standalone  run gRPC server as a container (standalone deploy)."
      echo "                Without --docker: gateway in-memory/external-storage fallback."
      echo "                With --docker: gateway + local storage containers."
      echo "  --build       ensure Python package (maturin) + release gRPC binary are built"
      echo "  --no-spawn    disable subtask spawning (sets UC_NO_SPAWN=1)."
      echo "                Hard-blocks UC uc_task submit + /uc submit + submit_task RPC."
      echo "                OMP task tool is a SOFT constraint — to also block it, set"
      echo "                task.disabledAgents in ~/.omp/agent/config.yml."
      echo "  --no-dashboard  skip dashboard startup (dashboard starts by default)."
      echo "                  Dashboard: Vite :5173 (http://localhost:5173) + FastAPI :8080."
      exit 0
      ;;
    *) OMP_ARGS+=("$arg") ;;
  esac
done

# ── --no-spawn: disable UC subtask dispatch ────────────────────
if [ "$NO_SPAWN" = true ]; then
    export UC_NO_SPAWN=1
    echo ">>> --no-spawn: UC subtask dispatch disabled (UC_NO_SPAWN=1)"
    echo ">>>   OMP 'task' tool is NOT hard-blocked. To also disable it, set"
    echo ">>>   task.disabledAgents in ~/.omp/agent/config.yml (OMP soft constraint)."
fi

# ── Build Python package if requested ────────────────────────
if [ "$DO_BUILD" = true ] && [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    "$SCRIPT_DIR/.venv/bin/python3" -c "import ultimate_coders" 2>/dev/null || {
        echo ">>> Building ultimate_coders Python package..."
        cd "$SCRIPT_DIR" && maturin develop --manifest-path crates/uc-python/Cargo.toml
    }
fi
# Ensure the release gRPC binary exists (build once, reuse across launches)
ensure_server_bin

# ── Standalone mode: gateway runs in a container, skip local binary path ──
# ponytail: delegate to run-gateway.sh instead of duplicating compose logic.
# --docker here means "container gateway + local storage containers".
if [ "$STANDALONE" = true ]; then
    if [ "$START_SERVER" = true ]; then
        GW_ARGS=(up)
        [ "$USE_DOCKER" = true ] && GW_ARGS+=(--docker)
        [ "$DO_BUILD" = true ] && GW_ARGS+=(--build)
        "$SCRIPT_DIR/run-gateway.sh" "${GW_ARGS[@]}"
    else
        echo ">>> --standalone --no-server: gateway not started (use run-gateway.sh up)"
    fi
    # Gateway container is managed by run-gateway.sh — do NOT down it on exit.
    # Jump straight to OMP, skipping the local server/docker-storage sections.
    setup_omp_workspace
    # LOG_DIR is set below in the non-standalone path; ensure it exists here too
    # so start_dashboard can write logs before the standalone exec.
    LOG_DIR="$SCRIPT_DIR/.logs"
    mkdir -p "$LOG_DIR"
    start_dashboard
    cd "$OMP_WORKSPACE"
    exec bun "$OMP_ENTRY" \
      --extension "$UC_EXT" \
      ${OMP_ARGS+"${OMP_ARGS[@]}"}
fi

# ── Start Docker storage backends ──────────────────────────────
if [ "$USE_DOCKER" = true ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo ">>> Error: docker not found — install Docker first" >&2
        exit 1
    fi
    # ponytail: skip port cleanup if Docker containers are already running
    # (killing Docker's port-forward processes crashes the daemon)
    if cd "$SCRIPT_DIR/docker" && docker compose ps -q 2>/dev/null | grep -q .; then
        echo ">>> Docker backends already running, skipping port cleanup"
    else
        for port in 4222 6333 6334 2379 5432; do
            if lsof -i :$port >/dev/null 2>&1; then
                pid=$(lsof -ti :$port 2>/dev/null || true)
                if [ -n "$pid" ]; then
                    echo ">>> Port :$port in use (PID $pid) — stopping"
                    kill $pid 2>/dev/null || true
                fi
            fi
        done
    fi
    sleep 1
    echo ">>> Starting Docker storage backends (TiKV, Qdrant, PG, NATS)..."
    cd "$SCRIPT_DIR/docker" && docker compose up -d pd tikv qdrant postgres nats
    echo "    Waiting for backends to be healthy..."
    for i in $(seq 1 60); do
        # ponytail: one `compose ps` call per tick (was two) — fork + dockerd
        # round-trip per call; halved across up to 60 ticks.
        ps_json=$(docker compose ps --format json 2>/dev/null || true)
        unhealthy=$(printf '%s' "$ps_json" | grep -c '"Health":"unhealthy"' || true)
        starting=$(printf '%s' "$ps_json" | grep -c '"Health":"starting"' || true)
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
    # Stop dashboard backend + Vite dev server (if started)
    for pid in "$DASH_PID" "$VITE_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo ">>> Stopping dashboard process (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    # Reap any remaining zombies
    reap_children
    if [ "$USE_DOCKER" = true ]; then
        echo ">>> Stopping Docker storage backends..."
        cd "$SCRIPT_DIR/docker" && docker compose down 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Health monitor: restart gRPC server if it dies
# ponytail: all output to log file — TUI owns the terminal after exec
health_monitor() {
    exec >> "$LOG_DIR/grpc-server.log" 2>&1
    while true; do
        sleep 10
        if [ -n "$SERVER_PID" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo ">>> gRPC server died (PID $SERVER_PID), restarting..."
            wait "$SERVER_PID" 2>/dev/null || true
            echo ">>> Last 10 lines:"
            tail -10 "$LOG_DIR/grpc-server.log" 2>/dev/null
            cd "$SCRIPT_DIR"
            if [ -f "$UC_ENV_FILE" ]; then
                set -a; source "$UC_ENV_FILE"; set +a
            fi
            "$SERVER_BIN" >> "$LOG_DIR/grpc-server.log" 2>&1 &
            SERVER_PID=$!
            echo ">>> Restarted gRPC server (PID $SERVER_PID)"
            date +%s > /tmp/uc-grpc-restart-marker
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
        # ponytail: write env file for gRPC server — ensures all subprocesses
        # (including health_monitor restarts) inherit the same configuration
        UC_ENV_FILE="$LOG_DIR/grpc-server.env"
        cat > "$UC_ENV_FILE" <<EOF
RUST_LOG=${RUST_LOG:-info}
UC_NATS_URL=${UC_NATS_URL:-nats://127.0.0.1:4222}
UC_CORS_MODE=${UC_CORS_MODE:-dev}
EOF
        if [ "$USE_DOCKER" = true ]; then
            cat >> "$UC_ENV_FILE" <<EOF
UC_TIKV_PD_ENDPOINTS=${UC_TIKV_PD_ENDPOINTS:-127.0.0.1:2379}
UC_QDRANT_URL=${UC_QDRANT_URL:-http://127.0.0.1:6334}
UC_PG_URL=${UC_PG_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}
UC_DATABASE_URL=${UC_DATABASE_URL:-postgresql://ultimate_coders:ultimate_coders@127.0.0.1:5432/ultimate_coders}
UC_TASK_BACKEND=${UC_TASK_BACKEND:-postgres}
EOF
        fi
        set -a; source "$UC_ENV_FILE"; set +a
        "$SERVER_BIN" >> "$LOG_DIR/grpc-server.log" 2>&1 &
        SERVER_PID=$!
        echo "    Server PID: $SERVER_PID"
        echo "    Logs: $LOG_DIR/grpc-server.log"
        # Write restart marker so UC Orchestrator can detect server start
        date +%s > /tmp/uc-grpc-restart-marker
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
setup_omp_workspace
start_dashboard
cd "$OMP_WORKSPACE"
exec bun "$OMP_ENTRY" \
  --extension "$UC_EXT" \
  ${OMP_ARGS+"${OMP_ARGS[@]}"}
