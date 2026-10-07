#!/bin/bash

# Usage: ./pr_gate.sh <command> [options]   (see: ./pr_gate.sh --help)
# Runs pr_gate.py with the repository's shared Python environment (.venv)

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(cd "$TOOL_DIR/../.." && pwd)/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Error: $PYTHON not found; run ./install.sh from the repository root"
    exit 1
fi

exec "$PYTHON" "$TOOL_DIR/pr_gate.py" "$@"
