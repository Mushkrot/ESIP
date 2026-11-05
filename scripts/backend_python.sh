#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" && -x "$ROOT_DIR/backend/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/backend/.venv/bin/python"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found. Follow SETUP.md from the repository root." >&2
  echo "Create it with: python3.12 -m venv .venv && .venv/bin/python -m pip install -r backend/requirements.txt" >&2
  exit 1
fi

export PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" "$@"
