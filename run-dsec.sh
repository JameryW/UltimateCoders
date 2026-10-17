#!/usr/bin/env bash
set -euo pipefail

# dsec sandbox environment — stack launcher.
#
# Runs the full UltimateCoders stack with every coding-agent slot pinned to
# the DeepSeek sandbox environment (UC_CODING_AGENT=dsec → dsh --profile
# headless). See docs/dsec-sandbox.md.
#
# Usage:
#   ./run-dsec.sh up [--build]   Start the stack (detached)
#   ./run-dsec.sh doctor         Boot diagnosis inside the worker image
#                                (no API cost, no other services needed)
#   ./run-dsec.sh down           Stop the stack
#   ./run-dsec.sh logs [svc]     Tail logs (all services or one)
#   ./run-dsec.sh status         Container status
#   ./run-dsec.sh --help
#
# Prerequisites:
#   docker/.env with DEEPSEEK_API_KEY — copy docker/.env.dsec.example.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_COMPOSE="$SCRIPT_DIR/docker/docker-compose.yml"
DSEC_COMPOSE="$SCRIPT_DIR/docker/docker-compose.dsec.yml"
ENV_FILE="$SCRIPT_DIR/docker/.env"
ENV_EXAMPLE="$SCRIPT_DIR/docker/.env.dsec.example"

ACTION=""
DO_BUILD=false
LOG_SERVICE=""

for arg in "$@"; do
  case "$arg" in
    up|down|doctor|logs|status) ACTION="$arg" ;;
    --build) DO_BUILD=true ;;
    worker|nats-worker|orchestrator|gateway|dashboard-ui)
      LOG_SERVICE="$arg" ;;
    --help|-h)
      cat <<EOF
Usage: $0 <action> [options]

Actions:
  up [--build]   Start the full stack with the dsec environment (detached).
                 Dashboard: http://localhost:8081/dashboard
  doctor         Run the dsec boot diagnosis (dsh + credentials + profile)
                 inside the worker image. No API cost; needs no other service.
  down           Stop and remove the stack containers.
  logs [svc]     Tail logs (all services, or one of: worker, nats-worker,
                 orchestrator, gateway, dashboard-ui).
  status         Show container status.

Options:
  --build        Rebuild images before starting (doctor always probes with
                 the current image, building it if missing).

Environment (docker/.env, git-ignored):
  DEEPSEEK_API_KEY   required — dsh authenticates from the environment
  DEEPSEEK_BASE_URL  optional — proxy/gateway endpoint override
  UC_CODING_AGENT    forced to "dsec" by docker/docker-compose.dsec.yml

First run:
  cp docker/.env.dsec.example docker/.env   # then set DEEPSEEK_API_KEY
  $0 up
EOF
      exit 0 ;;
    *) echo "Unknown arg: $arg (try --help)" >&2; exit 1 ;;
  esac
done

[ -z "$ACTION" ] && { echo "Error: no action given (try --help)" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || { echo "Error: docker not found on PATH" >&2; exit 1; }

# ponytail: one compose invocation shape everywhere — base stack + dsec
# override, app profile. The override pins UC_CODING_AGENT=dsec on every
# agent-spawning service, so no other profile juggling is needed.
COMPOSE_ARGS=(-f "$BASE_COMPOSE" -f "$DSEC_COMPOSE" --profile app)

warn_missing_env() {
  if [ ! -f "$ENV_FILE" ]; then
    echo ">>> Note: $ENV_FILE not found — copy $ENV_EXAMPLE and set DEEPSEEK_API_KEY" >&2
    return
  fi
  # ponytail: grep the key's presence, not its value — never echo secrets.
  if ! grep -q '^DEEPSEEK_API_KEY=..*' "$ENV_FILE"; then
    echo ">>> Note: DEEPSEEK_API_KEY is empty in $ENV_FILE — dsh tasks will fail to authenticate" >&2
  fi
}

case "$ACTION" in
  up)
    warn_missing_env
    if [ "$DO_BUILD" = true ]; then
      echo ">>> Building images..."
      docker compose "${COMPOSE_ARGS[@]}" build
    fi
    echo ">>> Starting stack with the dsec sandbox environment (UC_CODING_AGENT=dsec)..."
    docker compose "${COMPOSE_ARGS[@]}" up -d
    echo ">>> Stack up. Dashboard: http://localhost:8081/dashboard"
    echo ">>> Verify the environment anytime with: $0 doctor"
    ;;
  doctor)
    # ponytail: probe with the current worker image (build if absent). The
    # worker image already ships dsh (@deepseek-ai/dsh), so this exercises
    # the real binary, the real headless profile and the real credentials
    # path — with zero API cost.
    echo ">>> dsec doctor (boot diagnosis inside the worker image)..."
    docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps --build \
      -e DEEPSEEK_API_KEY -e DEEPSEEK_BASE_URL \
      worker python -m ultimate_coders.agent.sandbox_dsec --check
    ;;
  down)
    echo ">>> Stopping stack..."
    docker compose "${COMPOSE_ARGS[@]}" down --remove-orphans
    ;;
  logs)
    if [ -n "$LOG_SERVICE" ]; then
      docker compose "${COMPOSE_ARGS[@]}" logs -f --tail 50 "$LOG_SERVICE"
    else
      docker compose "${COMPOSE_ARGS[@]}" logs -f --tail 50
    fi
    ;;
  status)
    docker compose "${COMPOSE_ARGS[@]}" ps
    ;;
esac
