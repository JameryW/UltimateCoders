#!/bin/bash
# Run omp with UC Orchestrator extension loaded
cd "$(dirname "$0")/vendor/oh-my-pi"
exec bun packages/coding-agent/src/cli.ts \
  --extension ../../packages/uc-orchestrator \
  "$@"
