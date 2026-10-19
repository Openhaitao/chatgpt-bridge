#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${BRIDGE_PYTHON:-}" ]; then
    PYTHON=$BRIDGE_PYTHON
elif [ -x "$HOME/.local/lib/claude-chatgpt/venv/bin/python3" ]; then
    PYTHON="$HOME/.local/lib/claude-chatgpt/venv/bin/python3"
else
    PYTHON=python3
fi
cd "$ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m unittest discover -s tests -v
/bin/sh -n "$ROOT/claude-astra"
