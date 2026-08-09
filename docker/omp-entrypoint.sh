#!/bin/sh
set -eu

OMP_ROOT="/opt/oh-my-pi"
WORKSPACE="${UC_OMP_WORKSPACE:-/workspace/UltimateCoders}"

mkdir -p "$WORKSPACE/.omp" "$WORKSPACE/.claude"

# Keep OMP's project discovery rooted at the mounted workspace while sharing
# the vendored command/skill packs from the image.
if [ -d "$OMP_ROOT/.omp/commands" ]; then
  ln -sfn "$OMP_ROOT/.omp/commands" "$WORKSPACE/.omp/commands"
fi
if [ -d "$OMP_ROOT/.omp/skills" ]; then
  ln -sfn "$OMP_ROOT/.omp/skills" "$WORKSPACE/.omp/skills"
fi

cd "$WORKSPACE"
exec bun "$OMP_ROOT/packages/coding-agent/src/cli.ts" \
    "$@"
