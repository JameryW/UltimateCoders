#!/usr/bin/env bash
# Run the UltimateCoders TUI.
#
# Usage:
#   ./scripts/run_tui.sh          # dev mode (tsx watch)
#   ./scripts/run_tui.sh --build  # build + run dist/cli.js
#   ./scripts/run_tui.sh --grpc   # also start gRPC server in background
#
# Environment:
#   GRPC_SERVER_ADDR  gRPC server address (default: localhost:50051)
#   GRPC_PROTO_PATH   path to engine.proto (default: auto-resolve)
set -euo pipefail

cd "$(dirname "$0")/.."
TUI_DIR="tui"

# ── Ensure dependencies ──────────────────────────────────────
if [ ! -d "$TUI_DIR/node_modules" ]; then
  echo "📦 Installing TUI dependencies..."
  cd "$TUI_DIR" && npm install && cd ..
fi

# ── Parse flags ───────────────────────────────────────────────
MODE="dev"
START_GRPC=false

for arg in "$@"; do
  case "$arg" in
    --build) MODE="build" ;;
    --grpc)  START_GRPC=true ;;
    --help|-h)
      echo "Usage: $0 [--build] [--grpc]"
      echo ""
      echo "  (default)  dev mode with tsx watch"
      echo "  --build    build with esbuild, then run dist/cli.js"
      echo "  --grpc     start gRPC server in background first"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

# ── Start gRPC server if requested ────────────────────────────
GRPC_PID=""
GRPC_LOG=""
if [ "$START_GRPC" = true ]; then
  GRPC_LOG="/tmp/uc-grpc-server-$(date +%s).log"
  echo "🚀 Starting gRPC server..."
  echo "   Log: $GRPC_LOG"
  RUST_LOG=info cargo run -p uc-grpc-server >"$GRPC_LOG" 2>&1 &
  GRPC_PID=$!
  echo "   PID: $GRPC_PID (will stop on exit)"
  # Wait for server to be ready (check log for bind message)
  for i in $(seq 1 20); do
    if grep -q "started" "$GRPC_LOG" 2>/dev/null || grep -q "listening" "$GRPC_LOG" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
fi

cleanup() {
  if [ -n "$GRPC_PID" ]; then
    echo ""
    echo "🛑 Stopping gRPC server (PID $GRPC_PID)..."
    kill "$GRPC_PID" 2>/dev/null || true
    wait "$GRPC_PID" 2>/dev/null || true
  fi
  if [ -n "$GRPC_LOG" ] && [ -f "$GRPC_LOG" ]; then
    echo "📋 gRPC server log saved: $GRPC_LOG"
  fi
}
trap cleanup EXIT

# ── Run TUI ───────────────────────────────────────────────────
cd "$TUI_DIR"

case "$MODE" in
  dev)
    echo "🖥️  Starting TUI in dev mode (tsx watch)..."
    npx tsx watch src/index.tsx
    ;;
  build)
    echo "🏗️  Building TUI..."
    node build.mjs
    echo "🖥️  Starting TUI..."
    node dist/cli.js
    ;;
esac
