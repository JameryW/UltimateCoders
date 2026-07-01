#!/usr/bin/env bash
set -euo pipefail

# Run all test suites: Rust + Python + TypeScript.
# Exits non-zero if any suite fails.
#
# Usage: ./test-all.sh [--rust-only | --python-only | --ts-only]
#   no flag  → run all three

cd "$(dirname "$0")"

run_rust() {
	echo "=== Rust tests (cargo test --workspace) ==="
	cargo test --workspace
}

run_python() {
	echo "=== Python tests (pytest) ==="
	if [ -x .venv/bin/pytest ]; then
		.venv/bin/pytest tests/python/
	else
		echo "⚠ .venv/bin/pytest not found; falling back to system pytest"
		PYTHONPATH=python pytest tests/python/
	fi
}

run_ts() {
	echo "=== TypeScript tests (bun test) ==="
	( cd packages/uc-orchestrator && bun test )
}

case "${1:-all}" in
	--rust-only)   run_rust ;;
	--python-only) run_python ;;
	--ts-only)     run_ts ;;
	all)
		run_rust
		run_python
		run_ts
		echo "=== All suites done ==="
		;;
	*)
		echo "Usage: $0 [--rust-only | --python-only | --ts-only]" >&2
		exit 2
		;;
esac
