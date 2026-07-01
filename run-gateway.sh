#!/usr/bin/env bash
set -euo pipefail

# Standalone gateway container manager.
#
# Two modes:
#   default             — gateway container only, storage in-memory fallback
#                         (docker/docker-compose.gateway.yml)
#   --docker            — gateway + local storage containers (TiKV/Qdrant/PG/NATS)
#                         via docker/docker-compose.yml --profile gateway
#
# Usage:
#   ./run-gateway.sh up [--docker] [--build]
#   ./run-gateway.sh down [--docker]
#   ./run-gateway.sh logs
#   ./run-gateway.sh status
#   ./run-gateway.sh --help
#
# Storage-external deployment: export UC_TIKV_PD_ENDPOINTS / UC_QDRANT_URL /
# UC_PG_URL / UC_NATS_URL before `up` (default mode) to point at remote backends
# instead of in-memory fallback.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_COMPOSE="$SCRIPT_DIR/docker/docker-compose.gateway.yml"
FULL_COMPOSE="$SCRIPT_DIR/docker/docker-compose.yml"

ACTION=""
USE_DOCKER=false
DO_BUILD=false

for arg in "$@"; do
  case "$arg" in
    up|down|logs|status) ACTION="$arg" ;;
    --docker)  USE_DOCKER=true ;;
    --build)   DO_BUILD=true ;;
    --help|-h)
      cat <<EOF
Usage: $0 <action> [options]

Actions:
  up        Start the standalone gateway container
  down      Stop and remove the gateway container (+ storage if --docker)
  logs      Tail gateway logs
  status    Show container status

Options:
  --docker  Also start local storage containers (TiKV/Qdrant/PG/NATS);
            gateway connects to them via the compose network.
            Without this, gateway runs in-memory fallback (or external
            storage if UC_*_URL env vars are set).
  --build   Rebuild the gateway image before starting.

Env (default mode, storage-external):
  UC_TIKV_PD_ENDPOINTS, UC_QDRANT_URL, UC_PG_URL, UC_NATS_URL
  — set these to point at remote backends; empty = in-memory fallback.
EOF
      exit 0 ;;
    *) echo "Unknown arg: $arg (try --help)" >&2; exit 1 ;;
  esac
done

[ -z "$ACTION" ] && { echo "Error: no action given (try --help)" >&2; exit 1; }

# ponytail: pick compose file + profile by mode.
# --docker  → full compose with gateway profile (gateway + storage, same network,
#             gateway env already wired to service names pd/tikv/qdrant/postgres/nats)
# default   → gateway-only compose (storage external or in-memory fallback)
if [ "$USE_DOCKER" = true ]; then
  COMPOSE_ARGS=(-f "$FULL_COMPOSE" --profile gateway)
  COMPOSE_DOWN_ARGS=(-f "$FULL_COMPOSE" --profile gateway)
else
  COMPOSE_ARGS=(-f "$GATEWAY_COMPOSE")
  COMPOSE_DOWN_ARGS=(-f "$GATEWAY_COMPOSE")
fi
# ponytail: --remove-orphans is a `down` subcommand flag, must follow `down`,
# not sit in the compose options array.
DOWN_EXTRA=(--remove-orphans)

case "$ACTION" in
  up)
    if [ "$DO_BUILD" = true ]; then
      docker compose "${COMPOSE_ARGS[@]}" build
    fi
    # ponytail: free port 50051 if a non-docker host process holds it.
    # A running gateway container holds 50051 via Docker's vpnkit forwarder
    # (process name com.docker) — never kill that, it crashes the daemon.
    # A host binary (uc-grpc-server) holding it must yield to the container.
    if ! docker compose "${COMPOSE_ARGS[@]}" ps -q gateway 2>/dev/null | grep -q .; then
      pid=$(lsof -ti :50051 2>/dev/null | head -1 || true)
      if [ -n "$pid" ]; then
        pname=$(ps -p "$pid" -o comm= 2>/dev/null | head -1)
        if [ "$pname" != "com.docker" ] && [ "$pname" != "com.docke" ]; then
          echo ">>> Port :50051 held by host process $pname (PID $pid) — stopping"
          kill "$pid" 2>/dev/null || true
          sleep 1
        fi
      fi
    fi
    echo ">>> Starting standalone gateway ($([ "$USE_DOCKER" = true ] && echo 'with local storage' || echo 'in-memory/external storage'))..."
    docker compose "${COMPOSE_ARGS[@]}" up -d
    echo ">>> Waiting for gateway on :50051..."
    for i in $(seq 1 30); do
      if lsof -i :50051 >/dev/null 2>&1; then
        echo "    Gateway ready on :50051"
        exit 0
      fi
      sleep 1
    done
    echo ">>> Gateway not ready after 30s — check: docker compose ${COMPOSE_ARGS[*]} logs" >&2
    exit 1
    ;;
  down)
    echo ">>> Stopping standalone gateway..."
    docker compose "${COMPOSE_DOWN_ARGS[@]}" down "${DOWN_EXTRA[@]}"
    ;;
  logs)
    docker compose "${COMPOSE_ARGS[@]}" logs -f --tail 50
    ;;
  status)
    docker compose "${COMPOSE_ARGS[@]}" ps
    ;;
esac
