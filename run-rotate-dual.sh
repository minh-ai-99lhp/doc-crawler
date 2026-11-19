#!/usr/bin/env bash
# Launcher mỏng cho supervisor dual-lane; toàn bộ state machine nằm trong Python.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$DIR/.venv/bin/python}"
exec "$PYTHON_BIN" "$DIR/dual_rotation.py"
