#!/usr/bin/env bash
set -euo pipefail

# Run OMP with UC Orchestrator extension loaded
# This is the primary way to interact with UltimateCoders.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load local environment (API keys, base URLs, model overrides)
# ponytail: .env is gitignored, safe for secrets
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

cd "$SCRIPT_DIR/vendor/oh-my-pi"
exec bun packages/coding-agent/src/cli.ts \
  --extension ../../packages/uc-orchestrator \
  "$@"
