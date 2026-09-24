#!/usr/bin/env bash
# Strict mode is confined to THIS child script, never the caller's SSH shell.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON="${PYTHON:-python3}"; fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m mdlmm.cli "$@" --root "$ROOT"
